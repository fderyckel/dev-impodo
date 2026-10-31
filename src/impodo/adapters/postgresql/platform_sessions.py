"""Persist bounded shared-server session transitions for every principal kind."""

from __future__ import annotations

import hmac
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
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    CurrentSessionView,
    RevokeServerSessionCommand,
    ServerSessionConflictError,
    ServerSessionError,
    ServerSessionRecord,
    ServerSessionStorageError,
    ServerSessionUnavailableError,
    SessionAuthenticationMethod,
    SessionProof,
    SessionRevocationReason,
    SessionSecretMaterial,
    SessionSecurityEvent,
    SessionState,
    SessionSubject,
)

POSTGRESQL_SESSION_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_SESSION_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_SESSION_STATEMENT_TIMEOUT_MILLISECONDS = 10_000
POSTGRESQL_SESSION_CONNECTION_BUDGET = 1

_LOCK_PLATFORM_SUBJECT_QUERY: LiteralString = (
    "SELECT status, security_revision\n"
    "FROM impodo_control.platform_administrator\n"
    "WHERE administrator_id = %s\n"
    "FOR UPDATE"
)
_LOCK_ORGANIZATION_ADMINISTRATOR_QUERY: LiteralString = (
    "SELECT u.status, u.security_revision, o.status\n"
    "FROM impodo_control.customer_user AS u\n"
    "JOIN impodo_control.organization AS o\n"
    "  ON o.organization_id = u.organization_id\n"
    "JOIN impodo_control.organization_administrator_assignment AS a\n"
    "  ON a.organization_id = u.organization_id AND a.user_id = u.user_id\n"
    "WHERE u.user_id = %s AND u.organization_id = %s\n"
    "  AND u.principal_kind = 'ORGANIZATION_ADMINISTRATOR'\n"
    "FOR UPDATE OF u, o, a"
)
_LOCK_CUSTOMER_USER_QUERY: LiteralString = (
    "SELECT u.status, u.security_revision, o.status\n"
    "FROM impodo_control.customer_user AS u\n"
    "JOIN impodo_control.organization AS o ON o.organization_id = u.organization_id\n"
    "WHERE u.user_id = %s AND u.organization_id = %s\n"
    "  AND u.principal_kind = 'CUSTOMER_USER'\n"
    "FOR UPDATE OF u, o"
)
_INSERT_SESSION_QUERY: LiteralString = (
    "INSERT INTO impodo_control.server_session (\n"
    "    session_id, principal_kind, principal_id, organization_id,\n"
    "    handle_digest, csrf_digest, authentication_method,\n"
    "    security_revision, state, created_at, last_seen_at,\n"
    "    idle_expires_at, absolute_expires_at, elevated_until,\n"
    "    optimistic_revision\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, "
    "%s, %s, %s, %s)"
)
_INSERT_SESSION_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.platform_security_event (\n"
    "    event_id, deployment_id, event_kind, subject_administrator_id,\n"
    "    actor_administrator_id, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s)"
)
_INSERT_CUSTOMER_SESSION_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.server_security_event (\n"
    "    event_id, deployment_id, event_kind, principal_kind, principal_id,\n"
    "    organization_id, principal_hint_digest, source_digest, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s)"
)
_LOAD_SESSION_SUBJECT_QUERY: LiteralString = (
    "SELECT principal_kind, principal_id, organization_id\n"
    "FROM impodo_control.server_session\n"
    "WHERE session_id = %s"
)
_LOAD_SESSION_QUERY: LiteralString = (
    "SELECT s.principal_kind, s.principal_id, s.organization_id,\n"
    "       s.handle_digest, s.csrf_digest, s.authentication_method,\n"
    "       s.security_revision, s.state, s.created_at, s.last_seen_at,\n"
    "       s.idle_expires_at, s.absolute_expires_at, s.elevated_until,\n"
    "       s.optimistic_revision\n"
    "FROM impodo_control.server_session AS s\n"
    "WHERE s.session_id = %s\n"
    "FOR UPDATE OF s"
)
_TOUCH_SESSION_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET last_seen_at = %s, idle_expires_at = %s,\n"
    "    elevated_until = CASE WHEN elevated_until > %s "
    "THEN elevated_until ELSE NULL END,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE session_id = %s AND state = 'ACTIVE' "
    "AND optimistic_revision = %s"
)
_ROTATE_SESSION_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET handle_digest = %s, csrf_digest = %s, authentication_method = %s,\n"
    "    last_seen_at = %s, idle_expires_at = %s, elevated_until = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE session_id = %s AND state = 'ACTIVE' "
    "AND optimistic_revision = %s"
)
_REVOKE_SESSION_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET state = 'REVOKED', revoked_at = %s, revocation_reason = %s,\n"
    "    elevated_until = NULL, optimistic_revision = optimistic_revision + 1\n"
    "WHERE session_id = %s AND state = 'ACTIVE' "
    "AND optimistic_revision = %s"
)


class PostgreSQLServerSession(Protocol):
    """Expose one short-lived server-session transaction."""

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


class PostgreSQLServerSessionFactory(Protocol):
    """Open one runtime-role server-session transaction without a pool."""

    def __call__(self, dsn: str) -> PostgreSQLServerSession: ...


class _PsycopgServerSession:
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


class _MeasuredServerSession:
    def __init__(self, session: PostgreSQLServerSession) -> None:
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


def open_psycopg_server_session(dsn: str) -> PostgreSQLServerSession:
    """Open one bounded runtime-role transaction for session facts."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_SESSION_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_SESSION_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=10000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_SESSION_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-server-session",
        options=options,
    )
    return _PsycopgServerSession(connection)


_Result = TypeVar("_Result")


class PsycopgServerSessionRepository:
    """Use one connection and transaction per session operation."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLServerSessionFactory = open_psycopg_server_session,
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def create(
        self,
        record: ServerSessionRecord,
        event: SessionSecurityEvent,
    ) -> None:
        """Create one active session only for the locked current subject state."""

        def persist(session: _MeasuredServerSession) -> None:
            subject = _lock_subject(session, record.subject)
            if (
                subject is None
                or not subject[0]
                or subject[1] != record.security_revision
            ):
                raise ServerSessionConflictError("Session subject changed")
            session.execute(_INSERT_SESSION_QUERY, _record_parameters(record))
            _insert_session_event(session, event, record.subject)

        self._run("server_session.create", persist)

    def resolve(
        self,
        *,
        proof: SessionProof,
        accessed_at: datetime,
        proposed_idle_expires_at: datetime,
    ) -> CurrentSessionView | None:
        """Resolve, validate, and touch one opaque session in one transaction."""

        def resolve_one(session: _MeasuredServerSession) -> CurrentSessionView | None:
            context = _load_session_context(session, proof.session_id)
            if (
                context is None
                or context.state is not SessionState.ACTIVE
                or not _proof_matches(context, proof)
            ):
                return None
            reason = _rejection_reason(context, accessed_at)
            if reason is not None:
                _revoke_loaded(session, context, reason, accessed_at)
                return None
            new_idle_expires_at = min(
                proposed_idle_expires_at,
                context.absolute_expires_at,
            )
            if (
                session.execute_affected(
                    _TOUCH_SESSION_QUERY,
                    (
                        accessed_at,
                        new_idle_expires_at,
                        accessed_at,
                        context.session_id,
                        context.optimistic_revision,
                    ),
                )
                != 1
            ):
                raise ServerSessionConflictError("Session changed during access")
            return context.view(
                last_seen_at=accessed_at,
                idle_expires_at=new_idle_expires_at,
                elevated_until=(
                    context.elevated_until
                    if context.elevated_until is not None
                    and context.elevated_until > accessed_at
                    else None
                ),
                optimistic_revision=context.optimistic_revision + 1,
            )

        return self._run("server_session.resolve", resolve_one)

    def rotate(
        self,
        *,
        proof: SessionProof,
        replacement: SessionSecretMaterial,
        authentication_method: SessionAuthenticationMethod,
        expected_security_revision: int,
        rotated_at: datetime,
        proposed_idle_expires_at: datetime,
        elevated_until: datetime,
        event: SessionSecurityEvent,
    ) -> CurrentSessionView | None:
        """Rotate both browser secrets and establish bounded elevation."""

        def rotate_one(session: _MeasuredServerSession) -> CurrentSessionView | None:
            context = _load_session_context(session, proof.session_id)
            if (
                context is None
                or context.state is not SessionState.ACTIVE
                or replacement.session_id != proof.session_id
                or not _proof_matches(context, proof)
            ):
                return None
            reason = _rejection_reason(context, rotated_at)
            if reason is not None:
                _revoke_loaded(session, context, reason, rotated_at)
                return None
            if expected_security_revision != context.security_revision:
                return None
            new_idle_expires_at = min(
                proposed_idle_expires_at,
                context.absolute_expires_at,
            )
            new_elevated_until = min(elevated_until, context.absolute_expires_at)
            if (
                session.execute_affected(
                    _ROTATE_SESSION_QUERY,
                    (
                        replacement.handle_digest,
                        replacement.csrf_digest,
                        authentication_method.value,
                        rotated_at,
                        new_idle_expires_at,
                        new_elevated_until,
                        context.session_id,
                        context.optimistic_revision,
                    ),
                )
                != 1
            ):
                raise ServerSessionConflictError("Session changed during rotation")
            _insert_session_event(session, event, context.subject)
            return context.view(
                authentication_method=authentication_method,
                last_seen_at=rotated_at,
                idle_expires_at=new_idle_expires_at,
                elevated_until=new_elevated_until,
                optimistic_revision=context.optimistic_revision + 1,
            )

        return self._run("server_session.rotate", rotate_one)

    def revoke(
        self,
        command: RevokeServerSessionCommand,
        event: SessionSecurityEvent,
        *,
        revoked_at: datetime,
    ) -> None:
        """Revoke one exact self-session under its current subject binding."""

        def revoke_one(session: _MeasuredServerSession) -> None:
            context = _load_session_context(session, command.session_id)
            if (
                context is None
                or context.state is not SessionState.ACTIVE
                or context.subject != command.actor
                or not context.subject_active
                or context.optimistic_revision != command.expected_revision
            ):
                raise ServerSessionConflictError("Session changed before revocation")
            _revoke_loaded(session, context, command.reason, revoked_at)
            _insert_session_event(session, event, context.subject)

        self._run("server_session.revoke", revoke_one)

    def _run(
        self,
        operation_name: str,
        operation: Callable[[_MeasuredServerSession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise ServerSessionUnavailableError("Server sessions are unavailable") from error

        measured = _MeasuredServerSession(session)
        started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured)
                measured.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except ServerSessionError:
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
                raise ServerSessionUnavailableError(
                    "Server sessions are unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise ServerSessionStorageError("Server session statement failed") from error
        finally:
            active_error = sys.exception()
            duration_milliseconds = (perf_counter() - started) * 1000
            try:
                measured.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise ServerSessionUnavailableError(
                        "Server sessions are unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation=operation_name,
                        outcome=outcome,
                        connection_count=POSTGRESQL_SESSION_CONNECTION_BUDGET,
                        query_count=measured.query_count,
                        pool_wait_milliseconds=0.0,
                        transaction_duration_milliseconds=duration_milliseconds,
                        retry_count=0,
                    ),
                )


class _SessionContext:
    def __init__(
        self,
        *,
        session_id: str,
        subject: SessionSubject,
        handle_digest: bytes,
        csrf_digest: bytes,
        authentication_method: SessionAuthenticationMethod,
        security_revision: int,
        state: SessionState,
        created_at: datetime,
        last_seen_at: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        elevated_until: datetime | None,
        optimistic_revision: int,
        subject_active: bool,
        current_security_revision: int,
    ) -> None:
        self.session_id = session_id
        self.subject = subject
        self.handle_digest = handle_digest
        self.csrf_digest = csrf_digest
        self.authentication_method = authentication_method
        self.security_revision = security_revision
        self.state = state
        self.created_at = created_at
        self.last_seen_at = last_seen_at
        self.idle_expires_at = idle_expires_at
        self.absolute_expires_at = absolute_expires_at
        self.elevated_until = elevated_until
        self.optimistic_revision = optimistic_revision
        self.subject_active = subject_active
        self.current_security_revision = current_security_revision

    def view(
        self,
        *,
        authentication_method: SessionAuthenticationMethod | None = None,
        last_seen_at: datetime | None = None,
        idle_expires_at: datetime | None = None,
        elevated_until: datetime | None = None,
        optimistic_revision: int | None = None,
    ) -> CurrentSessionView:
        return CurrentSessionView(
            session_id=self.session_id,
            subject=self.subject,
            authentication_method=authentication_method or self.authentication_method,
            security_revision=self.security_revision,
            created_at=self.created_at,
            last_seen_at=last_seen_at or self.last_seen_at,
            idle_expires_at=idle_expires_at or self.idle_expires_at,
            absolute_expires_at=self.absolute_expires_at,
            elevated_until=elevated_until,
            optimistic_revision=optimistic_revision or self.optimistic_revision,
        )


def _lock_subject(
    session: _MeasuredServerSession,
    subject: SessionSubject,
) -> tuple[bool, int] | None:
    if subject.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
        row = session.fetch_one(
            _LOCK_PLATFORM_SUBJECT_QUERY,
            (subject.principal_id,),
        )
        if row is None:
            return None
        try:
            if len(row) != 2:
                raise TypeError
            return _string(row[0]) == "ACTIVE", _integer(row[1])
        except (TypeError, ValueError) as error:
            raise ServerSessionStorageError(
                "Session subject state is invalid"
            ) from error
    if subject.principal_kind in {
        LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        LoginPrincipalKind.CUSTOMER_USER,
    }:
        if subject.organization_id is None:
            return None
        row = session.fetch_one(
            _LOCK_ORGANIZATION_ADMINISTRATOR_QUERY
            if subject.principal_kind is LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            else _LOCK_CUSTOMER_USER_QUERY,
            (subject.principal_id, subject.organization_id),
        )
        if row is None:
            return None
        try:
            if len(row) != 3:
                raise TypeError
            return (
                _string(row[0]) == "ACTIVE" and _string(row[2]) == "ACTIVE",
                _integer(row[1]),
            )
        except (TypeError, ValueError) as error:
            raise ServerSessionStorageError(
                "Session subject state is invalid"
            ) from error
    return None


def _load_session_context(
    session: _MeasuredServerSession,
    session_id: str,
) -> _SessionContext | None:
    subject = _decode_session_subject(
        session.fetch_one(_LOAD_SESSION_SUBJECT_QUERY, (session_id,))
    )
    if subject is None:
        return None
    subject_state = _lock_subject(session, subject)
    if subject_state is None:
        return None
    return _decode_session_context(
        session_id,
        session.fetch_one(_LOAD_SESSION_QUERY, (session_id,)),
        expected_subject=subject,
        subject_state=subject_state,
    )


def _decode_session_subject(
    row: tuple[object, ...] | None,
) -> SessionSubject | None:
    if row is None:
        return None
    try:
        if len(row) != 3:
            raise TypeError
        return SessionSubject(
            principal_kind=LoginPrincipalKind(_string(row[0])),
            principal_id=str(row[1]),
            organization_id=None if row[2] is None else _string(row[2]),
        )
    except (TypeError, ValueError, ServerSessionError) as error:
        raise ServerSessionStorageError("Stored session subject is invalid") from error


def _decode_session_context(
    session_id: str,
    row: tuple[object, ...] | None,
    *,
    expected_subject: SessionSubject,
    subject_state: tuple[bool, int],
) -> _SessionContext | None:
    if row is None:
        return None
    try:
        if len(row) != 14:
            raise TypeError
        principal_kind = LoginPrincipalKind(_string(row[0]))
        principal_id = str(row[1])
        organization_id = None if row[2] is None else _string(row[2])
        handle_digest = _bytes(row[3])
        csrf_digest = _bytes(row[4])
        authentication_method = SessionAuthenticationMethod(_string(row[5]))
        security_revision = _integer(row[6])
        state = SessionState(_string(row[7]))
        created_at = _datetime(row[8])
        last_seen_at = _datetime(row[9])
        idle_expires_at = _datetime(row[10])
        absolute_expires_at = _datetime(row[11])
        elevated_until = None if row[12] is None else _datetime(row[12])
        optimistic_revision = _integer(row[13])
        subject = SessionSubject(
            principal_kind=principal_kind,
            principal_id=principal_id,
            organization_id=organization_id,
        )
        if subject != expected_subject:
            raise TypeError
        return _SessionContext(
            session_id=session_id,
            subject=subject,
            handle_digest=handle_digest,
            csrf_digest=csrf_digest,
            authentication_method=authentication_method,
            security_revision=security_revision,
            state=state,
            created_at=created_at,
            last_seen_at=last_seen_at,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            elevated_until=elevated_until,
            optimistic_revision=optimistic_revision,
            subject_active=subject_state[0],
            current_security_revision=subject_state[1],
        )
    except (TypeError, ValueError, ServerSessionError) as error:
        raise ServerSessionStorageError("Stored server session is invalid") from error


def _proof_matches(context: _SessionContext, proof: SessionProof) -> bool:
    if not hmac.compare_digest(context.handle_digest, proof.handle_digest):
        return False
    return proof.csrf_digest is None or hmac.compare_digest(
        context.csrf_digest,
        proof.csrf_digest,
    )


def _rejection_reason(
    context: _SessionContext,
    observed_at: datetime,
) -> SessionRevocationReason | None:
    if context.state is not SessionState.ACTIVE:
        return None
    if not context.subject_active:
        return SessionRevocationReason.ACCOUNT_SUSPENDED
    if context.security_revision != context.current_security_revision:
        return SessionRevocationReason.SECURITY_REVISION
    if observed_at >= context.idle_expires_at or observed_at >= context.absolute_expires_at:
        return SessionRevocationReason.EXPIRED
    return None


def _revoke_loaded(
    session: _MeasuredServerSession,
    context: _SessionContext,
    reason: SessionRevocationReason,
    revoked_at: datetime,
) -> None:
    if context.state is not SessionState.ACTIVE:
        return
    if (
        session.execute_affected(
            _REVOKE_SESSION_QUERY,
            (
                revoked_at,
                reason.value,
                context.session_id,
                context.optimistic_revision,
            ),
        )
        != 1
    ):
        raise ServerSessionConflictError("Session changed during revocation")


def _record_parameters(record: ServerSessionRecord) -> tuple[object, ...]:
    return (
        record.session_id,
        record.subject.principal_kind.value,
        record.subject.principal_id,
        record.subject.organization_id,
        record.handle_digest,
        record.csrf_digest,
        record.authentication_method.value,
        record.security_revision,
        record.created_at,
        record.last_seen_at,
        record.idle_expires_at,
        record.absolute_expires_at,
        record.elevated_until,
        record.optimistic_revision,
    )


def _insert_session_event(
    session: _MeasuredServerSession,
    event: SessionSecurityEvent,
    subject: SessionSubject,
) -> None:
    if subject.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
        actor_id = (
            event.actor.principal_id
            if event.actor is not None
            and event.actor.principal_kind
            is LoginPrincipalKind.PLATFORM_ADMINISTRATOR
            else None
        )
        session.execute(
            _INSERT_SESSION_EVENT_QUERY,
            (
                event.event_id,
                event.deployment_id,
                event.kind.value,
                subject.principal_id,
                actor_id,
                event.occurred_at,
            ),
        )
        return
    session.execute(
        _INSERT_CUSTOMER_SESSION_EVENT_QUERY,
        (
            event.event_id,
            event.deployment_id,
            event.kind.value,
            subject.principal_kind.value,
            subject.principal_id,
            subject.organization_id,
            event.occurred_at,
        ),
    )


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _bytes(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError
    return value


def _rollback_or_unavailable(session: PostgreSQLServerSession) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise ServerSessionUnavailableError("Server sessions are unavailable") from error


__all__ = [
    "POSTGRESQL_SESSION_CONNECTION_BUDGET",
    "PostgreSQLServerSession",
    "PostgreSQLServerSessionFactory",
    "PsycopgServerSessionRepository",
    "open_psycopg_server_session",
]
