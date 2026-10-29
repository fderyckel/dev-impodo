"""Persist bounded Platform Administrator recovery-token transitions."""

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
from impodo.domain.identity.bootstrap import PlatformAdministratorStatus
from impodo.domain.identity.recovery import (
    AccountRecoveryError,
    AccountRecoveryRejectedError,
    AccountRecoveryStorageError,
    AccountRecoverySubjectNotEligibleError,
    AccountRecoveryUnavailableError,
    AuthorizedRecoveryTokenCommand,
    LoginPrincipalKind,
    PasswordResetMutation,
    RecoverySecurityEvent,
    RecoveryTokenIssue,
    RecoveryTokenPolicy,
    RecoveryTokenPurpose,
)

POSTGRESQL_RECOVERY_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_RECOVERY_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_RECOVERY_STATEMENT_TIMEOUT_MILLISECONDS = 10_000
POSTGRESQL_RECOVERY_CONNECTION_BUDGET = 1

_LOAD_RESET_CONTEXT_QUERY: LiteralString = (
    "SELECT a.administrator_id, a.status, a.security_revision, "
    "p.password_revision,\n"
    "       (EXISTS (\n"
    "            SELECT 1 FROM impodo_control.authentication_throttle AS ta\n"
    "            WHERE ta.scope_kind = 'RESET_ACCOUNT' "
    "AND ta.scope_digest = %s\n"
    "              AND ta.locked_until > %s\n"
    "        ) OR EXISTS (\n"
    "            SELECT 1 FROM impodo_control.authentication_throttle AS ts\n"
    "            WHERE ts.scope_kind = 'RESET_SOURCE' "
    "AND ts.scope_digest = %s\n"
    "              AND ts.locked_until > %s\n"
    "        )) AS throttled\n"
    "FROM (VALUES (1)) AS anchor(value)\n"
    "LEFT JOIN impodo_control.login_identity AS li\n"
    "  ON li.normalized_login_email = %s\n"
    " AND li.principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    "LEFT JOIN impodo_control.platform_administrator AS a\n"
    "  ON a.administrator_id = li.principal_id\n"
    "LEFT JOIN impodo_control.platform_administrator_password_identity AS p\n"
    "  ON p.administrator_id = a.administrator_id"
)
_LOCK_PLATFORM_SUBJECT_QUERY: LiteralString = (
    "SELECT a.status, a.security_revision, p.password_revision\n"
    "FROM impodo_control.platform_administrator AS a\n"
    "JOIN impodo_control.platform_administrator_password_identity AS p\n"
    "  ON p.administrator_id = a.administrator_id\n"
    "JOIN impodo_control.login_identity AS li\n"
    "  ON li.principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    " AND li.principal_id = a.administrator_id\n"
    "WHERE a.administrator_id = %s\n"
    "FOR UPDATE OF a, p"
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
_REVOKE_OUTSTANDING_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET state = 'REVOKED', revoked_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE principal_kind = %s AND principal_id = %s AND purpose = %s\n"
    "  AND state = 'ISSUED'"
)
_INSERT_TOKEN_QUERY: LiteralString = (
    "INSERT INTO impodo_control.account_recovery_token (\n"
    "    token_id, principal_kind, principal_id, purpose, token_digest, state,\n"
    "    issued_security_revision, issued_password_revision,\n"
    "    issued_by_administrator_id, issued_at, expires_at, optimistic_revision\n"
    ") VALUES (%s, %s, %s, %s, %s, 'ISSUED', %s, %s, %s, %s, %s, 1)"
)
_INSERT_SECURITY_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.platform_security_event (\n"
    "    event_id, deployment_id, event_kind, subject_administrator_id,\n"
    "    principal_hint_digest, source_digest, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
_LOAD_RESET_TOKEN_QUERY: LiteralString = (
    "SELECT a.administrator_id, a.status, a.security_revision,\n"
    "       p.password_revision, t.issued_security_revision,\n"
    "       t.issued_password_revision, t.optimistic_revision\n"
    "FROM impodo_control.account_recovery_token AS t\n"
    "JOIN impodo_control.login_identity AS li\n"
    "  ON li.principal_kind = t.principal_kind\n"
    " AND li.principal_id = t.principal_id\n"
    "JOIN impodo_control.platform_administrator AS a\n"
    "  ON t.principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    " AND a.administrator_id = t.principal_id\n"
    "JOIN impodo_control.platform_administrator_password_identity AS p\n"
    "  ON p.administrator_id = a.administrator_id\n"
    "WHERE t.token_id = %s AND t.token_digest = %s\n"
    "  AND t.purpose = 'PASSWORD_RESET' AND t.state = 'ISSUED'\n"
    "  AND t.expires_at > %s\n"
    "FOR UPDATE OF a, p, t"
)
_UPDATE_PASSWORD_QUERY: LiteralString = (
    "UPDATE impodo_control.platform_administrator_password_identity\n"
    "SET algorithm = %s, parameters = %s::jsonb, salt = %s,\n"
    "    password_hash = %s, password_revision = password_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE administrator_id = %s AND password_revision = %s"
)
_UPDATE_SECURITY_REVISION_QUERY: LiteralString = (
    "UPDATE impodo_control.platform_administrator\n"
    "SET security_revision = security_revision + 1\n"
    "WHERE administrator_id = %s AND security_revision = %s"
)
_CONSUME_TOKEN_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET state = 'CONSUMED', consumed_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE token_id = %s AND token_digest = %s AND state = 'ISSUED'\n"
    "  AND optimistic_revision = %s"
)
_REVOKE_OTHER_TOKENS_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET state = 'REVOKED', revoked_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    "  AND principal_id = %s AND token_id <> %s AND state = 'ISSUED'"
)
_REVOKE_PASSWORD_RESET_SESSIONS_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET state = 'REVOKED', revoked_at = %s,\n"
    "    revocation_reason = 'PASSWORD_RESET', elevated_until = NULL,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE principal_kind = 'PLATFORM_ADMINISTRATOR'\n"
    "  AND principal_id = %s AND state = 'ACTIVE'"
)


class PostgreSQLAccountRecoverySession(Protocol):
    """Expose one short-lived account-recovery transaction."""

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


class PostgreSQLAccountRecoverySessionFactory(Protocol):
    """Open one runtime-role recovery transaction without a pool."""

    def __call__(self, dsn: str) -> PostgreSQLAccountRecoverySession: ...


class _PsycopgAccountRecoverySession:
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


class _MeasuredAccountRecoverySession:
    def __init__(self, session: PostgreSQLAccountRecoverySession) -> None:
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


def open_psycopg_account_recovery_session(
    dsn: str,
) -> PostgreSQLAccountRecoverySession:
    """Open one bounded runtime-role transaction for recovery facts."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_RECOVERY_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_RECOVERY_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=10000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_RECOVERY_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-account-recovery",
        options=options,
    )
    return _PsycopgAccountRecoverySession(connection)


_Result = TypeVar("_Result")


class _PasswordResetNotCompleted(AccountRecoveryRejectedError):
    pass


class PsycopgAccountRecoveryRepository:
    """Use one transaction per issue, request, or consumption operation."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLAccountRecoverySessionFactory = (
            open_psycopg_account_recovery_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def request_password_reset(
        self,
        *,
        normalized_login_email: str,
        account_digest: str,
        source_digest: str,
        issue: RecoveryTokenIssue,
        request_event: RecoverySecurityEvent,
        issue_event: RecoverySecurityEvent,
        policy: RecoveryTokenPolicy,
    ) -> bool:
        """Throttle every request and issue only for an active exact subject."""

        def request(session: _MeasuredAccountRecoverySession) -> bool:
            row = session.fetch_one(
                _LOAD_RESET_CONTEXT_QUERY,
                (
                    account_digest,
                    issue.issued_at,
                    source_digest,
                    issue.issued_at,
                    normalized_login_email,
                ),
            )
            context = _decode_reset_context(row)
            for scope_kind, scope_digest in (
                ("RESET_ACCOUNT", account_digest),
                ("RESET_SOURCE", source_digest),
            ):
                session.execute(
                    _UPSERT_THROTTLE_QUERY,
                    _throttle_parameters(
                        scope_kind,
                        scope_digest,
                        issue.issued_at,
                        policy,
                    ),
                )
            session.execute(
                _INSERT_SECURITY_EVENT_QUERY,
                _event_parameters(request_event, subject=None),
            )
            if context is None or context[4]:
                return False
            (
                administrator_id,
                status,
                _security_revision,
                _password_revision,
                _throttled,
            ) = context
            if status is not PlatformAdministratorStatus.ACTIVE:
                return False
            locked = _decode_locked_subject(
                session.fetch_one(_LOCK_PLATFORM_SUBJECT_QUERY, (administrator_id,))
            )
            if locked is None or locked[0] is not PlatformAdministratorStatus.ACTIVE:
                return False
            _persist_issue(
                session,
                principal_id=administrator_id,
                issue=issue,
                security_revision=locked[1],
                password_revision=locked[2],
                issued_by_administrator_id=None,
            )
            session.execute(
                _INSERT_SECURITY_EVENT_QUERY,
                _event_parameters(issue_event, subject=administrator_id),
            )
            return True

        return self._run("account_recovery.request_reset", request)

    def issue_authorized(
        self,
        command: AuthorizedRecoveryTokenCommand,
        issue: RecoveryTokenIssue,
        event: RecoverySecurityEvent,
    ) -> None:
        """Issue an exact-subject reset token or reject unsupported activation."""

        if (
            command.principal_kind is not LoginPrincipalKind.PLATFORM_ADMINISTRATOR
            or command.purpose is RecoveryTokenPurpose.ACTIVATION
        ):
            raise AccountRecoverySubjectNotEligibleError(
                "The exact recovery-token subject is not eligible"
            )

        def persist(session: _MeasuredAccountRecoverySession) -> None:
            locked = _decode_locked_subject(
                session.fetch_one(
                    _LOCK_PLATFORM_SUBJECT_QUERY,
                    (command.principal_id,),
                )
            )
            if locked is None or locked[0] is not PlatformAdministratorStatus.ACTIVE:
                raise AccountRecoverySubjectNotEligibleError(
                    "The exact recovery-token subject is not eligible"
                )
            _persist_issue(
                session,
                principal_id=command.principal_id,
                issue=issue,
                security_revision=locked[1],
                password_revision=locked[2],
                issued_by_administrator_id=command.principal_id,
            )
            session.execute(
                _INSERT_SECURITY_EVENT_QUERY,
                _event_parameters(event, subject=command.principal_id),
            )

        self._run("account_recovery.issue_authorized", persist)

    def reset_password(
        self,
        mutation: PasswordResetMutation,
        event: RecoverySecurityEvent,
    ) -> bool:
        """Atomically consume the token and invalidate existing sessions."""

        def reset(session: _MeasuredAccountRecoverySession) -> None:
            row = session.fetch_one(
                _LOAD_RESET_TOKEN_QUERY,
                (
                    mutation.proof.token_id,
                    mutation.proof.digest,
                    mutation.occurred_at,
                ),
            )
            context = _decode_consumption_context(row)
            if context is None:
                raise _PasswordResetNotCompleted("Account recovery did not complete")
            (
                administrator_id,
                status,
                security_revision,
                password_revision,
                issued_security_revision,
                issued_password_revision,
                token_revision,
            ) = context
            if (
                status is not PlatformAdministratorStatus.ACTIVE
                or security_revision != issued_security_revision
                or password_revision != issued_password_revision
            ):
                raise _PasswordResetNotCompleted("Account recovery did not complete")
            replacement = mutation.replacement_hash
            if (
                session.execute_affected(
                    _UPDATE_PASSWORD_QUERY,
                    (
                        replacement.algorithm,
                        json.dumps(
                            dict(replacement.parameters),
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        replacement.salt,
                        replacement.digest,
                        mutation.occurred_at,
                        administrator_id,
                        password_revision,
                    ),
                )
                != 1
                or session.execute_affected(
                    _UPDATE_SECURITY_REVISION_QUERY,
                    (administrator_id, security_revision),
                )
                != 1
                or session.execute_affected(
                    _CONSUME_TOKEN_QUERY,
                    (
                        mutation.occurred_at,
                        mutation.proof.token_id,
                        mutation.proof.digest,
                        token_revision,
                    ),
                )
                != 1
            ):
                raise _PasswordResetNotCompleted("Account recovery did not complete")
            session.execute(
                _REVOKE_PASSWORD_RESET_SESSIONS_QUERY,
                (mutation.occurred_at, administrator_id),
            )
            session.execute(
                _REVOKE_OTHER_TOKENS_QUERY,
                (
                    mutation.occurred_at,
                    administrator_id,
                    mutation.proof.token_id,
                ),
            )
            session.execute(
                _INSERT_SECURITY_EVENT_QUERY,
                _event_parameters(event, subject=administrator_id),
            )

        try:
            self._run("account_recovery.reset_password", reset)
        except _PasswordResetNotCompleted:
            return False
        return True

    def _run(
        self,
        operation_name: str,
        operation: Callable[[_MeasuredAccountRecoverySession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise AccountRecoveryUnavailableError(
                "Account recovery is unavailable"
            ) from error

        measured = _MeasuredAccountRecoverySession(session)
        started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured)
                measured.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except AccountRecoveryError:
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
                raise AccountRecoveryUnavailableError(
                    "Account recovery is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise AccountRecoveryStorageError(
                    "Account recovery statement failed"
                ) from error
        finally:
            active_error = sys.exception()
            duration_milliseconds = (perf_counter() - started) * 1000
            try:
                measured.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise AccountRecoveryUnavailableError(
                        "Account recovery is unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation=operation_name,
                        outcome=outcome,
                        connection_count=POSTGRESQL_RECOVERY_CONNECTION_BUDGET,
                        query_count=measured.query_count,
                        pool_wait_milliseconds=0.0,
                        transaction_duration_milliseconds=duration_milliseconds,
                        retry_count=0,
                    ),
                )


def _decode_reset_context(
    row: tuple[object, ...] | None,
) -> tuple[str, PlatformAdministratorStatus, int, int, bool] | None:
    if row is None or len(row) != 5 or not isinstance(row[4], bool):
        raise AccountRecoveryStorageError("Reset request lookup is invalid")
    if row[0] is None:
        return None
    try:
        if not isinstance(row[1], str):
            raise TypeError
        if not isinstance(row[2], int) or not isinstance(row[3], int):
            raise TypeError
        return (
            str(row[0]),
            PlatformAdministratorStatus(row[1]),
            row[2],
            row[3],
            row[4],
        )
    except (TypeError, ValueError) as error:
        raise AccountRecoveryStorageError("Reset request lookup is invalid") from error


def _decode_locked_subject(
    row: tuple[object, ...] | None,
) -> tuple[PlatformAdministratorStatus, int, int] | None:
    if row is None:
        return None
    try:
        if len(row) != 3 or not isinstance(row[0], str):
            raise TypeError
        if not isinstance(row[1], int) or not isinstance(row[2], int):
            raise TypeError
        return PlatformAdministratorStatus(row[0]), row[1], row[2]
    except (TypeError, ValueError) as error:
        raise AccountRecoveryStorageError("Recovery subject is invalid") from error


def _decode_consumption_context(
    row: tuple[object, ...] | None,
) -> tuple[str, PlatformAdministratorStatus, int, int, int, int, int] | None:
    if row is None:
        return None
    try:
        if len(row) != 7 or not isinstance(row[1], str):
            raise TypeError
        security_revision = row[2]
        password_revision = row[3]
        issued_security_revision = row[4]
        issued_password_revision = row[5]
        token_revision = row[6]
        if (
            not isinstance(security_revision, int)
            or not isinstance(password_revision, int)
            or not isinstance(issued_security_revision, int)
            or not isinstance(issued_password_revision, int)
            or not isinstance(token_revision, int)
        ):
            raise TypeError
        return (
            str(row[0]),
            PlatformAdministratorStatus(row[1]),
            security_revision,
            password_revision,
            issued_security_revision,
            issued_password_revision,
            token_revision,
        )
    except (TypeError, ValueError) as error:
        raise AccountRecoveryStorageError("Recovery token state is invalid") from error


def _persist_issue(
    session: _MeasuredAccountRecoverySession,
    *,
    principal_id: str,
    issue: RecoveryTokenIssue,
    security_revision: int,
    password_revision: int,
    issued_by_administrator_id: str | None,
) -> None:
    if issue.purpose is not RecoveryTokenPurpose.PASSWORD_RESET:
        raise AccountRecoverySubjectNotEligibleError(
            "The exact recovery-token subject is not eligible"
        )
    session.execute(
        _REVOKE_OUTSTANDING_QUERY,
        (
            issue.issued_at,
            LoginPrincipalKind.PLATFORM_ADMINISTRATOR.value,
            principal_id,
            issue.purpose.value,
        ),
    )
    session.execute(
        _INSERT_TOKEN_QUERY,
        (
            issue.token_id,
            LoginPrincipalKind.PLATFORM_ADMINISTRATOR.value,
            principal_id,
            issue.purpose.value,
            issue.digest,
            security_revision,
            password_revision,
            issued_by_administrator_id,
            issue.issued_at,
            issue.expires_at,
        ),
    )


def _event_parameters(
    event: RecoverySecurityEvent,
    *,
    subject: str | None,
) -> tuple[object, ...]:
    return (
        event.event_id,
        event.deployment_id,
        event.kind,
        subject,
        event.principal_hint_digest,
        event.source_digest,
        event.occurred_at,
    )


def _throttle_parameters(
    scope_kind: str,
    scope_digest: str,
    occurred_at: datetime,
    policy: RecoveryTokenPolicy,
) -> tuple[object, ...]:
    throttle = policy.reset_throttle
    window_seconds = int(throttle.failure_window.total_seconds())
    lockout_seconds = int(throttle.lockout_duration.total_seconds())
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
        throttle.maximum_failures,
        occurred_at,
        lockout_seconds,
        occurred_at,
    )


def _rollback_or_unavailable(session: PostgreSQLAccountRecoverySession) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise AccountRecoveryUnavailableError(
            "Account recovery is unavailable"
        ) from error


__all__ = [
    "POSTGRESQL_RECOVERY_CONNECTION_BUDGET",
    "PostgreSQLAccountRecoverySession",
    "PostgreSQLAccountRecoverySessionFactory",
    "PsycopgAccountRecoveryRepository",
    "open_psycopg_account_recovery_session",
]
