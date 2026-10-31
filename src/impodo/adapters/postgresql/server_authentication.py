"""Persist global login facts for platform and tenant-bound customer identities."""

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
from impodo.domain.identity.authentication import AuthenticationThrottlePolicy
from impodo.domain.identity.bootstrap import PasswordHash
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.server_authentication import (
    ServerAuthenticationAttempt,
    ServerAuthenticationCredential,
    ServerAuthenticationError,
    ServerAuthenticationSecurityEvent,
    ServerAuthenticationStorageError,
    ServerAuthenticationUnavailableError,
)

POSTGRESQL_SERVER_AUTH_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_SERVER_AUTH_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_SERVER_AUTH_STATEMENT_TIMEOUT_MILLISECONDS = 10_000
POSTGRESQL_SERVER_AUTH_CONNECTION_BUDGET = 1

_LOAD_ATTEMPT_QUERY: LiteralString = (
    "SELECT li.principal_kind, li.principal_id, u.organization_id,\n"
    "       COALESCE(a.security_revision, u.security_revision),\n"
    "       COALESCE(ap.algorithm, up.algorithm),\n"
    "       COALESCE(ap.parameters, up.parameters),\n"
    "       COALESCE(ap.salt, up.salt),\n"
    "       COALESCE(ap.password_hash, up.password_hash),\n"
    "       COALESCE(ap.password_revision, up.password_revision),\n"
    "       CASE\n"
    "         WHEN li.principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    "           THEN a.status = 'ACTIVE'\n"
    "         WHEN li.principal_kind = 'ORGANIZATION_ADMINISTRATOR'\n"
    "           THEN u.status = 'ACTIVE' AND o.status = 'ACTIVE'\n"
    "             AND assignment.user_id IS NOT NULL\n"
    "         WHEN li.principal_kind = 'CUSTOMER_USER'\n"
    "           THEN u.status = 'ACTIVE' AND o.status = 'ACTIVE'\n"
    "         ELSE false\n"
    "       END AS eligible,\n"
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
    " AND li.principal_kind IN "
    "('PLATFORM_ADMINISTRATOR', 'ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER')\n"
    "LEFT JOIN impodo_control.platform_administrator AS a\n"
    "  ON li.principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    " AND a.administrator_id = li.principal_id\n"
    "LEFT JOIN impodo_control.platform_administrator_password_identity AS ap\n"
    "  ON ap.administrator_id = a.administrator_id\n"
    "LEFT JOIN impodo_control.customer_user AS u\n"
    "  ON li.principal_kind IN ('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER')\n"
    " AND u.user_id = li.principal_id\n"
    " AND u.principal_kind = li.principal_kind\n"
    "LEFT JOIN impodo_control.customer_user_password_identity AS up\n"
    "  ON up.user_id = u.user_id\n"
    "LEFT JOIN impodo_control.organization AS o\n"
    "  ON o.organization_id = u.organization_id\n"
    "LEFT JOIN impodo_control.organization_administrator_assignment "
    "AS assignment\n"
    "  ON assignment.organization_id = u.organization_id\n"
    " AND assignment.user_id = u.user_id"
)
_INSERT_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.server_security_event (\n"
    "    event_id, deployment_id, event_kind, principal_kind, principal_id,\n"
    "    organization_id, principal_hint_digest, source_digest, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
_REHASH_PLATFORM_PASSWORD_QUERY: LiteralString = (
    "UPDATE impodo_control.platform_administrator_password_identity\n"
    "SET algorithm = %s, parameters = %s::jsonb, salt = %s,\n"
    "    password_hash = %s, password_revision = password_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE administrator_id = %s AND password_revision = %s"
)
_REHASH_CUSTOMER_PASSWORD_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user_password_identity\n"
    "SET algorithm = %s, parameters = %s::jsonb, salt = %s,\n"
    "    password_hash = %s, password_revision = password_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE user_id = %s AND password_revision = %s"
)


class PostgreSQLServerAuthenticationSession(Protocol):
    """Expose one short-lived generic authentication transaction."""

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


class PostgreSQLServerAuthenticationSessionFactory(Protocol):
    """Open one runtime-role generic authentication transaction."""

    def __call__(self, dsn: str) -> PostgreSQLServerAuthenticationSession: ...


class _PsycopgServerAuthenticationSession:
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


class _MeasuredSession:
    def __init__(self, session: PostgreSQLServerAuthenticationSession) -> None:
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


def open_psycopg_server_authentication_session(
    dsn: str,
) -> PostgreSQLServerAuthenticationSession:
    """Open one bounded runtime-role transaction for global login facts."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_SERVER_AUTH_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_SERVER_AUTH_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=10000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_SERVER_AUTH_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-server-authentication",
        options=options,
    )
    return _PsycopgServerAuthenticationSession(connection)


_Result = TypeVar("_Result")


class PsycopgServerAuthenticationRepository:
    """Use one connection and transaction per global login operation."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLServerAuthenticationSessionFactory = (
            open_psycopg_server_authentication_session
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
    ) -> ServerAuthenticationAttempt:
        """Read one supported credential and both throttle scopes."""

        def load(session: _MeasuredSession) -> ServerAuthenticationAttempt:
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

        return self._run("server_authentication.load", load)

    def record_rejection(
        self,
        event: ServerAuthenticationSecurityEvent,
        *,
        count_failure: bool,
        policy: AuthenticationThrottlePolicy,
    ) -> None:
        """Persist one generic rejection and advance both throttle scopes."""

        def record(session: _MeasuredSession) -> None:
            session.execute(_INSERT_EVENT_QUERY, _event_parameters(event))
            if count_failure:
                for scope_kind, scope_digest in (
                    ("ACCOUNT", event.principal_hint_digest),
                    ("SOURCE", event.source_digest),
                ):
                    session.execute(
                        _UPSERT_THROTTLE_QUERY,
                        _throttle_parameters(scope_kind, scope_digest, event, policy),
                    )

        self._run("server_authentication.reject", record)

    def record_success(
        self,
        success_event: ServerAuthenticationSecurityEvent,
        *,
        expected_password_revision: int,
        replacement_hash: PasswordHash | None,
        rehash_event: ServerAuthenticationSecurityEvent | None,
    ) -> bool:
        """Reset throttles, append success, and CAS a stale password hash."""

        def record(session: _MeasuredSession) -> bool:
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
                if (
                    success_event.principal_kind is None
                    or success_event.principal_id is None
                    or rehash_event is None
                ):
                    raise ServerAuthenticationStorageError(
                        "Server authentication rehash bundle is invalid"
                    )
                query = (
                    _REHASH_PLATFORM_PASSWORD_QUERY
                    if success_event.principal_kind
                    is LoginPrincipalKind.PLATFORM_ADMINISTRATOR
                    else _REHASH_CUSTOMER_PASSWORD_QUERY
                )
                rehashed = session.execute_affected(
                    query,
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
                        success_event.principal_id,
                        expected_password_revision,
                    ),
                ) == 1
                if rehashed:
                    session.execute(_INSERT_EVENT_QUERY, _event_parameters(rehash_event))
            session.execute(_INSERT_EVENT_QUERY, _event_parameters(success_event))
            return rehashed

        return self._run("server_authentication.succeed", record)

    def _run(
        self,
        operation_name: str,
        operation: Callable[[_MeasuredSession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise ServerAuthenticationUnavailableError(
                "Server authentication is unavailable"
            ) from error

        measured = _MeasuredSession(session)
        started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured)
                measured.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except ServerAuthenticationError:
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
                raise ServerAuthenticationUnavailableError(
                    "Server authentication is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise ServerAuthenticationStorageError(
                    "Server authentication statement failed"
                ) from error
        finally:
            active_error = sys.exception()
            duration_milliseconds = (perf_counter() - started) * 1000
            try:
                measured.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise ServerAuthenticationUnavailableError(
                        "Server authentication is unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation=operation_name,
                        outcome=outcome,
                        connection_count=POSTGRESQL_SERVER_AUTH_CONNECTION_BUDGET,
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
) -> ServerAuthenticationAttempt:
    if row is None or len(row) != 11 or not isinstance(row[10], bool):
        raise ServerAuthenticationStorageError(
            "Server authentication lookup result is invalid"
        )
    if row[0] is None or row[4] is None:
        return ServerAuthenticationAttempt(
            account_digest=account_digest,
            source_digest=source_digest,
            credential=None,
            throttled=row[10],
        )
    try:
        parameters_value = row[5]
        if isinstance(parameters_value, str):
            parameters_value = json.loads(parameters_value)
        if not isinstance(parameters_value, dict):
            raise TypeError
        parameters: list[tuple[str, int]] = []
        for name, value in parameters_value.items():
            if not isinstance(name, str) or not isinstance(value, int):
                raise TypeError
            parameters.append((name, value))
        if (
            not isinstance(row[3], int)
            or not isinstance(row[4], str)
            or not isinstance(row[8], int)
            or not isinstance(row[9], bool)
        ):
            raise TypeError
        credential = ServerAuthenticationCredential(
            principal_kind=LoginPrincipalKind(_string(row[0])),
            principal_id=str(row[1]),
            organization_id=None if row[2] is None else _string(row[2]),
            eligible=row[9],
            security_revision=row[3],
            password_hash=PasswordHash(
                algorithm=row[4],
                parameters=tuple(parameters),
                salt=_bytes(row[6]),
                digest=_bytes(row[7]),
            ),
            password_revision=row[8],
        )
    except (TypeError, ValueError) as error:
        raise ServerAuthenticationStorageError(
            "Server authentication credential is invalid"
        ) from error
    return ServerAuthenticationAttempt(
        account_digest=account_digest,
        source_digest=source_digest,
        credential=credential,
        throttled=row[10],
    )


def _event_parameters(event: ServerAuthenticationSecurityEvent) -> tuple[object, ...]:
    return (
        event.event_id,
        event.deployment_id,
        event.kind.value,
        event.principal_kind.value if event.principal_kind else None,
        event.principal_id,
        event.organization_id,
        event.principal_hint_digest,
        event.source_digest,
        event.occurred_at,
    )


def _throttle_parameters(
    scope_kind: str,
    scope_digest: str,
    event: ServerAuthenticationSecurityEvent,
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


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TypeError


def _rollback_or_unavailable(session: PostgreSQLServerAuthenticationSession) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise ServerAuthenticationUnavailableError(
            "Server authentication is unavailable"
        ) from error


__all__ = [
    "POSTGRESQL_SERVER_AUTH_CONNECTION_BUDGET",
    "PostgreSQLServerAuthenticationSession",
    "PostgreSQLServerAuthenticationSessionFactory",
    "PsycopgServerAuthenticationRepository",
    "open_psycopg_server_authentication_session",
]
