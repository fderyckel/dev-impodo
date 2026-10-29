"""Persist the first Platform Administrator in one bounded transaction."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any, LiteralString, Protocol

import psycopg

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.startup import PostgreSQLDsnError, PostgreSQLDsnReader
from impodo.adapters.postgresql.transaction_metrics import (
    PostgreSQLTransactionMetric,
    PostgreSQLTransactionMetricObserver,
    PostgreSQLTransactionOutcome,
    report_postgresql_transaction_metric,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PlatformAdministratorAlreadyBootstrappedError,
    PlatformAdministratorBootstrapError,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorBootstrapStorageError,
    PlatformAdministratorBootstrapUnavailableError,
    PlatformAdministratorStatus,
)

POSTGRESQL_BOOTSTRAP_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_BOOTSTRAP_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_BOOTSTRAP_STATEMENT_TIMEOUT_MILLISECONDS = 10_000
POSTGRESQL_BOOTSTRAP_CONNECTION_BUDGET = 1
POSTGRESQL_BOOTSTRAP_QUERY_BUDGET = 6

_BOOTSTRAP_LOCK_NAMESPACE = 1_229_803_599
_BOOTSTRAP_LOCK_RESOURCE = 1_095_775_281
_LOCK_QUERY: LiteralString = "SELECT pg_advisory_xact_lock(%s, %s)"
_ACTIVE_ADMINISTRATOR_QUERY: LiteralString = (
    "SELECT administrator_id\n"
    "FROM impodo_control.platform_administrator\n"
    "WHERE status = 'ACTIVE'\n"
    "ORDER BY created_at, administrator_id\n"
    "LIMIT 1"
)
_INSERT_ADMINISTRATOR_QUERY: LiteralString = (
    "INSERT INTO impodo_control.platform_administrator (\n"
    "    administrator_id, normalized_login_email, display_name, status,\n"
    "    security_revision, created_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s)"
)
_INSERT_PASSWORD_QUERY: LiteralString = (
    "INSERT INTO impodo_control.platform_administrator_password_identity (\n"
    "    administrator_id, algorithm, parameters, salt, password_hash, created_at\n"
    ") VALUES (%s, %s, %s::jsonb, %s, %s, %s)"
)
_INSERT_LOGIN_IDENTITY_QUERY: LiteralString = (
    "INSERT INTO impodo_control.login_identity (\n"
    "    normalized_login_email, principal_kind, "
    "principal_id, platform_administrator_id, created_at\n"
    ") VALUES (%s, 'PLATFORM_ADMINISTRATOR', %s, %s, %s)"
)
_INSERT_SECURITY_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.platform_security_event (\n"
    "    event_id, deployment_id, event_kind, subject_administrator_id, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s)"
)


class PostgreSQLPlatformBootstrapSession(Protocol):
    """Expose one short-lived first-operator transaction."""

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None: ...

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgreSQLPlatformBootstrapSessionFactory(Protocol):
    """Open one runtime-role bootstrap transaction without retaining a pool."""

    def __call__(self, dsn: str) -> PostgreSQLPlatformBootstrapSession: ...


class _PsycopgPlatformBootstrapSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None:
        self._connection.execute(query, parameters)

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


class _MeasuredPlatformBootstrapSession:
    def __init__(self, session: PostgreSQLPlatformBootstrapSession) -> None:
        self._session = session
        self.query_count = 0

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None:
        self.query_count += 1
        self._session.execute(query, parameters)

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


def open_psycopg_platform_bootstrap_session(
    dsn: str,
) -> PostgreSQLPlatformBootstrapSession:
    """Open one bounded runtime-role transaction for operator bootstrap."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_BOOTSTRAP_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_BOOTSTRAP_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=10000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_BOOTSTRAP_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-platform-bootstrap",
        options=options,
    )
    return _PsycopgPlatformBootstrapSession(connection)


class PsycopgPlatformAdministratorBootstrapRepository:
    """Serialize and atomically persist the one-time bootstrap bundle."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLPlatformBootstrapSessionFactory = (
            open_psycopg_platform_bootstrap_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def bootstrap(
        self,
        record: PlatformAdministratorBootstrapRecord,
        event: BootstrapSecurityEvent,
    ) -> None:
        """Create the first active operator or refuse without partial writes."""

        if (
            record.status is not PlatformAdministratorStatus.ACTIVE
            or record.security_revision != 1
            or event.subject_administrator_id != record.administrator_id
            or event.occurred_at != record.created_at
        ):
            raise PlatformAdministratorBootstrapStorageError(
                "Platform Administrator bootstrap bundle is invalid"
            )

        def persist(session: _MeasuredPlatformBootstrapSession) -> None:
            session.fetch_one(
                _LOCK_QUERY,
                (_BOOTSTRAP_LOCK_NAMESPACE, _BOOTSTRAP_LOCK_RESOURCE),
            )
            if session.fetch_one(_ACTIVE_ADMINISTRATOR_QUERY) is not None:
                raise PlatformAdministratorAlreadyBootstrappedError(
                    "An active Platform Administrator already exists"
                )
            session.execute(
                _INSERT_ADMINISTRATOR_QUERY,
                (
                    record.administrator_id,
                    record.normalized_login_email,
                    record.display_name,
                    record.status.value,
                    record.security_revision,
                    record.created_at,
                ),
            )
            session.execute(
                _INSERT_LOGIN_IDENTITY_QUERY,
                (
                    record.normalized_login_email,
                    record.administrator_id,
                    record.administrator_id,
                    record.created_at,
                ),
            )
            session.execute(
                _INSERT_PASSWORD_QUERY,
                (
                    record.administrator_id,
                    record.password_hash.algorithm,
                    json.dumps(
                        dict(record.password_hash.parameters),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    record.password_hash.salt,
                    record.password_hash.digest,
                    record.created_at,
                ),
            )
            session.execute(
                _INSERT_SECURITY_EVENT_QUERY,
                (
                    event.event_id,
                    event.deployment_id,
                    event.kind.value,
                    event.subject_administrator_id,
                    event.occurred_at,
                ),
            )

        self._run(persist)

    def _run(
        self,
        operation: Callable[[_MeasuredPlatformBootstrapSession], None],
    ) -> None:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise PlatformAdministratorBootstrapUnavailableError(
                "Platform Administrator bootstrap is unavailable"
            ) from error

        measured = _MeasuredPlatformBootstrapSession(session)
        started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                operation(measured)
                measured.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
            except PlatformAdministratorBootstrapError:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise
            except psycopg.errors.UniqueViolation as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PlatformAdministratorAlreadyBootstrappedError(
                    "An active Platform Administrator already exists"
                ) from error
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
                raise PlatformAdministratorBootstrapUnavailableError(
                    "Platform Administrator bootstrap is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PlatformAdministratorBootstrapStorageError(
                    "Platform Administrator bootstrap statement failed"
                ) from error
        finally:
            active_error = sys.exception()
            duration_milliseconds = (perf_counter() - started) * 1000
            try:
                measured.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise PlatformAdministratorBootstrapUnavailableError(
                        "Platform Administrator bootstrap is unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation="platform_administrator.bootstrap",
                        outcome=outcome,
                        connection_count=POSTGRESQL_BOOTSTRAP_CONNECTION_BUDGET,
                        query_count=measured.query_count,
                        pool_wait_milliseconds=0.0,
                        transaction_duration_milliseconds=duration_milliseconds,
                        retry_count=0,
                    ),
                )


def _rollback_or_unavailable(
    session: PostgreSQLPlatformBootstrapSession,
) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise PlatformAdministratorBootstrapUnavailableError(
            "Platform Administrator bootstrap is unavailable"
        ) from error


__all__ = [
    "POSTGRESQL_BOOTSTRAP_CONNECTION_BUDGET",
    "POSTGRESQL_BOOTSTRAP_QUERY_BUDGET",
    "PostgreSQLPlatformBootstrapSession",
    "PostgreSQLPlatformBootstrapSessionFactory",
    "PsycopgPlatformAdministratorBootstrapRepository",
    "open_psycopg_platform_bootstrap_session",
]
