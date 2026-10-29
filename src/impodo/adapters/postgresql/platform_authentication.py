"""Persist bounded Platform Administrator authentication facts."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any, LiteralString, Protocol, TypeVar

import psycopg

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.startup import PostgreSQLDsnError, PostgreSQLDsnReader
from impodo.adapters.postgresql.transaction_metrics import (
    PostgreSQLTransactionMetric,
    PostgreSQLTransactionMetricObserver,
    PostgreSQLTransactionOutcome,
    report_postgresql_transaction_metric,
)
from impodo.domain.identity.authentication import (
    AuthenticationSecurityEvent,
    AuthenticationThrottlePolicy,
    PlatformAuthenticationAttempt,
    PlatformAuthenticationCredential,
    PlatformAuthenticationError,
    PlatformAuthenticationStorageError,
    PlatformAuthenticationUnavailableError,
)
from impodo.domain.identity.bootstrap import (
    PasswordHash,
    PlatformAdministratorStatus,
)

POSTGRESQL_AUTHENTICATION_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_AUTHENTICATION_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_AUTHENTICATION_STATEMENT_TIMEOUT_MILLISECONDS = 10_000
POSTGRESQL_AUTHENTICATION_CONNECTION_BUDGET = 1
POSTGRESQL_AUTHENTICATION_LOAD_QUERY_BUDGET = 1
POSTGRESQL_AUTHENTICATION_REJECTION_QUERY_BUDGET = 3
POSTGRESQL_AUTHENTICATION_SUCCESS_QUERY_BUDGET = 4

_LOAD_ATTEMPT_QUERY: LiteralString = (
    "SELECT a.administrator_id, a.status, a.security_revision,\n"
    "       p.algorithm, p.parameters, p.salt, p.password_hash,\n"
    "       p.password_revision,\n"
    "       (EXISTS (\n"
    "            SELECT 1 FROM impodo_control.authentication_throttle AS ta\n"
    "            WHERE ta.scope_kind = 'ACCOUNT' AND ta.scope_digest = %s\n"
    "              AND ta.locked_until > %s\n"
    "        ) OR EXISTS (\n"
    "            SELECT 1 FROM impodo_control.authentication_throttle AS ts\n"
    "            WHERE ts.scope_kind = 'SOURCE' AND ts.scope_digest = %s\n"
    "              AND ts.locked_until > %s\n"
    "        )) AS throttled\n"
    "FROM (VALUES (1)) AS anchor(value)\n"
    "LEFT JOIN impodo_control.login_identity AS li\n"
    "  ON li.normalized_login_email = %s\n"
    " AND li.principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    "LEFT JOIN impodo_control.platform_administrator AS a\n"
    "  ON a.administrator_id = li.platform_administrator_id\n"
    "LEFT JOIN impodo_control.platform_administrator_password_identity AS p\n"
    "  ON p.administrator_id = a.administrator_id"
)
_INSERT_SECURITY_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.platform_security_event (\n"
    "    event_id, deployment_id, event_kind, subject_administrator_id,\n"
    "    principal_hint_digest, source_digest, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
_UPSERT_THROTTLE_QUERY: LiteralString = (
    "INSERT INTO impodo_control.authentication_throttle (\n"
    "    scope_kind, scope_digest, failure_count, window_started_at,\n"
    "    locked_until, optimistic_revision, updated_at\n"
    ") VALUES (%s, %s, 1, %s, NULL, 1, %s)\n"
    "ON CONFLICT (scope_kind, scope_digest) DO UPDATE SET\n"
    "    failure_count = CASE\n"
    "        WHEN authentication_throttle.locked_until > %s\n"
    "            THEN authentication_throttle.failure_count\n"
    "        WHEN authentication_throttle.window_started_at\n"
    "             + make_interval(secs => %s) <= %s THEN 1\n"
    "        ELSE least(authentication_throttle.failure_count + 1, 1000)\n"
    "    END,\n"
    "    window_started_at = CASE\n"
    "        WHEN authentication_throttle.locked_until > %s\n"
    "            THEN authentication_throttle.window_started_at\n"
    "        WHEN authentication_throttle.window_started_at\n"
    "             + make_interval(secs => %s) <= %s THEN %s\n"
    "        ELSE authentication_throttle.window_started_at\n"
    "    END,\n"
    "    locked_until = CASE\n"
    "        WHEN authentication_throttle.locked_until > %s\n"
    "            THEN authentication_throttle.locked_until\n"
    "        WHEN authentication_throttle.window_started_at\n"
    "             + make_interval(secs => %s) <= %s THEN NULL\n"
    "        WHEN authentication_throttle.failure_count + 1 >= %s\n"
    "            THEN %s + make_interval(secs => %s)\n"
    "        ELSE NULL\n"
    "    END,\n"
    "    optimistic_revision = "
    "authentication_throttle.optimistic_revision + 1,\n"
    "    updated_at = %s"
)
_RESET_THROTTLES_QUERY: LiteralString = (
    "UPDATE impodo_control.authentication_throttle\n"
    "SET failure_count = 0, locked_until = NULL,\n"
    "    optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE (scope_kind = 'ACCOUNT' AND scope_digest = %s)\n"
    "   OR (scope_kind = 'SOURCE' AND scope_digest = %s)"
)
_REHASH_PASSWORD_QUERY: LiteralString = (
    "UPDATE impodo_control.platform_administrator_password_identity\n"
    "SET algorithm = %s, parameters = %s::jsonb, salt = %s,\n"
    "    password_hash = %s, password_revision = password_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE administrator_id = %s AND password_revision = %s"
)


class PostgreSQLPlatformAuthenticationSession(Protocol):
    """Expose one short-lived authentication transaction."""

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None: ...

    def execute_affected(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> int: ...

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgreSQLPlatformAuthenticationSessionFactory(Protocol):
    """Open one runtime-role authentication transaction without a pool."""

    def __call__(self, dsn: str) -> PostgreSQLPlatformAuthenticationSession: ...


class _PsycopgPlatformAuthenticationSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None:
        self._connection.execute(query, parameters)

    def execute_affected(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> int:
        return self._connection.execute(query, parameters).rowcount

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        row = self._connection.execute(query, parameters).fetchone()
        return tuple(row) if row is not None else None

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class _MeasuredPlatformAuthenticationSession:
    def __init__(self, session: PostgreSQLPlatformAuthenticationSession) -> None:
        self._session = session
        self.query_count = 0

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None:
        self.query_count += 1
        self._session.execute(query, parameters)

    def execute_affected(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> int:
        self.query_count += 1
        return self._session.execute_affected(query, parameters)

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        self.query_count += 1
        return self._session.fetch_one(query, parameters)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        self._session.close()


def open_psycopg_platform_authentication_session(
    dsn: str,
) -> PostgreSQLPlatformAuthenticationSession:
    """Open one bounded runtime-role transaction for platform login facts."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_AUTHENTICATION_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_AUTHENTICATION_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=10000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_AUTHENTICATION_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-platform-authentication",
        options=options,
    )
    return _PsycopgPlatformAuthenticationSession(connection)


_Result = TypeVar("_Result")


class PsycopgPlatformAuthenticationRepository:
    """Use independent bounded transactions around each login persistence step."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLPlatformAuthenticationSessionFactory = (
            open_psycopg_platform_authentication_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def load_attempt(
        self,
        *,
        normalized_login_email: str,
        account_digest: str,
        source_digest: str,
        attempted_at: datetime,
    ) -> PlatformAuthenticationAttempt:
        """Read the credential and both lock scopes in one query."""

        def load(
            session: _MeasuredPlatformAuthenticationSession,
        ) -> PlatformAuthenticationAttempt:
            row = session.fetch_one(
                _LOAD_ATTEMPT_QUERY,
                (
                    account_digest,
                    attempted_at,
                    source_digest,
                    attempted_at,
                    normalized_login_email,
                ),
            )
            return _decode_attempt(row, account_digest, source_digest)

        return self._run("platform_authentication.load", load)

    def record_rejection(
        self,
        event: AuthenticationSecurityEvent,
        *,
        count_failure: bool,
        policy: AuthenticationThrottlePolicy,
    ) -> None:
        """Append a generic rejection and atomically advance both throttles."""

        def record(session: _MeasuredPlatformAuthenticationSession) -> None:
            session.execute(_INSERT_SECURITY_EVENT_QUERY, _event_parameters(event))
            if count_failure:
                for scope_kind, scope_digest in (
                    ("ACCOUNT", event.principal_hint_digest),
                    ("SOURCE", event.source_digest),
                ):
                    session.execute(
                        _UPSERT_THROTTLE_QUERY,
                        _throttle_parameters(scope_kind, scope_digest, event, policy),
                    )

        self._run("platform_authentication.reject", record)

    def record_success(
        self,
        success_event: AuthenticationSecurityEvent,
        *,
        expected_password_revision: int,
        replacement_hash: PasswordHash | None,
        rehash_event: AuthenticationSecurityEvent | None,
    ) -> bool:
        """Reset throttles, append success, and compare-and-swap a stale hash."""

        def record(session: _MeasuredPlatformAuthenticationSession) -> bool:
            session.execute(
                _RESET_THROTTLES_QUERY,
                (
                    success_event.occurred_at,
                    success_event.principal_hint_digest,
                    success_event.source_digest,
                ),
            )
            rehashed = False
            if replacement_hash is not None:
                subject = success_event.subject_administrator_id
                if subject is None or rehash_event is None:
                    raise PlatformAuthenticationStorageError(
                        "Platform authentication rehash bundle is invalid"
                    )
                affected = session.execute_affected(
                    _REHASH_PASSWORD_QUERY,
                    (
                        replacement_hash.algorithm,
                        json.dumps(
                            dict(replacement_hash.parameters),
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        replacement_hash.salt,
                        replacement_hash.digest,
                        success_event.occurred_at,
                        subject,
                        expected_password_revision,
                    ),
                )
                rehashed = affected == 1
                if rehashed:
                    session.execute(
                        _INSERT_SECURITY_EVENT_QUERY,
                        _event_parameters(rehash_event),
                    )
            session.execute(
                _INSERT_SECURITY_EVENT_QUERY,
                _event_parameters(success_event),
            )
            return rehashed

        return self._run("platform_authentication.succeed", record)

    def _run(
        self,
        operation_name: str,
        operation: Callable[[_MeasuredPlatformAuthenticationSession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise PlatformAuthenticationUnavailableError(
                "Platform authentication is unavailable"
            ) from error

        measured = _MeasuredPlatformAuthenticationSession(session)
        started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured)
                measured.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except PlatformAuthenticationError:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise
            except (
                psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable,
                psycopg.errors.QueryCanceled,
                psycopg.errors.SerializationFailure,
                psycopg.InterfaceError,
                psycopg.OperationalError,
            ) as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PlatformAuthenticationUnavailableError(
                    "Platform authentication is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PlatformAuthenticationStorageError(
                    "Platform authentication statement failed"
                ) from error
        finally:
            active_error = sys.exception()
            duration_milliseconds = (perf_counter() - started) * 1000
            try:
                measured.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise PlatformAuthenticationUnavailableError(
                        "Platform authentication is unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation=operation_name,
                        outcome=outcome,
                        connection_count=POSTGRESQL_AUTHENTICATION_CONNECTION_BUDGET,
                        query_count=measured.query_count,
                        pool_wait_milliseconds=0.0,
                        transaction_duration_milliseconds=duration_milliseconds,
                        retry_count=0,
                    ),
                )


def _decode_attempt(
    row: tuple[object, ...] | None,
    account_digest: str,
    source_digest: str,
) -> PlatformAuthenticationAttempt:
    if row is None or len(row) != 9 or not isinstance(row[8], bool):
        raise PlatformAuthenticationStorageError(
            "Platform authentication lookup result is invalid"
        )
    if row[0] is None:
        return PlatformAuthenticationAttempt(
            account_digest=account_digest,
            source_digest=source_digest,
            credential=None,
            throttled=row[8],
        )
    try:
        parameters_value = row[4]
        if isinstance(parameters_value, str):
            parameters_value = json.loads(parameters_value)
        if not isinstance(parameters_value, dict):
            raise TypeError
        parameters: list[tuple[str, int]] = []
        for name, value in parameters_value.items():
            if not isinstance(name, str) or not isinstance(value, int):
                raise TypeError
            parameters.append((name, value))
        if not isinstance(row[1], str) or not isinstance(row[2], int):
            raise TypeError
        if not isinstance(row[3], str) or not isinstance(row[7], int):
            raise TypeError
        credential = PlatformAuthenticationCredential(
            administrator_id=str(row[0]),
            status=PlatformAdministratorStatus(row[1]),
            security_revision=row[2],
            password_hash=PasswordHash(
                algorithm=row[3],
                parameters=tuple(parameters),
                salt=_decode_bytes(row[5]),
                digest=_decode_bytes(row[6]),
            ),
            password_revision=row[7],
        )
    except (TypeError, ValueError) as error:
        raise PlatformAuthenticationStorageError(
            "Platform authentication credential is invalid"
        ) from error
    return PlatformAuthenticationAttempt(
        account_digest=account_digest,
        source_digest=source_digest,
        credential=credential,
        throttled=row[8],
    )


def _decode_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TypeError


def _event_parameters(event: AuthenticationSecurityEvent) -> tuple[object, ...]:
    return (
        event.event_id,
        event.deployment_id,
        event.kind,
        event.subject_administrator_id,
        event.principal_hint_digest,
        event.source_digest,
        event.occurred_at,
    )


def _throttle_parameters(
    scope_kind: str,
    scope_digest: str,
    event: AuthenticationSecurityEvent,
    policy: AuthenticationThrottlePolicy,
) -> tuple[object, ...]:
    occurred_at = event.occurred_at
    window_seconds = int(policy.failure_window.total_seconds())
    lockout_seconds = int(policy.lockout_duration.total_seconds())
    return (
        scope_kind,
        scope_digest,
        occurred_at,
        occurred_at,
        occurred_at,
        window_seconds,
        occurred_at,
        occurred_at,
        window_seconds,
        occurred_at,
        occurred_at,
        occurred_at,
        window_seconds,
        occurred_at,
        policy.maximum_failures,
        occurred_at,
        lockout_seconds,
        occurred_at,
    )


def _rollback_or_unavailable(
    session: PostgreSQLPlatformAuthenticationSession,
) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise PlatformAuthenticationUnavailableError(
            "Platform authentication is unavailable"
        ) from error


__all__ = [
    "POSTGRESQL_AUTHENTICATION_CONNECTION_BUDGET",
    "POSTGRESQL_AUTHENTICATION_LOAD_QUERY_BUDGET",
    "POSTGRESQL_AUTHENTICATION_REJECTION_QUERY_BUDGET",
    "POSTGRESQL_AUTHENTICATION_SUCCESS_QUERY_BUDGET",
    "PostgreSQLPlatformAuthenticationSession",
    "PostgreSQLPlatformAuthenticationSessionFactory",
    "PsycopgPlatformAuthenticationRepository",
    "open_psycopg_platform_authentication_session",
]
