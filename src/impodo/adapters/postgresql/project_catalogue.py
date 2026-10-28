"""Persist bounded Organization-scoped Project catalogue facts in PostgreSQL."""

from __future__ import annotations

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
from impodo.domain.project.catalogue import (
    ProjectCatalogueAlreadyExistsError,
    ProjectCatalogueCursor,
    ProjectCatalogueError,
    ProjectCatalogueListQuery,
    ProjectCatalogueNotFoundError,
    ProjectCataloguePage,
    ProjectCatalogueRecord,
    ProjectCatalogueRevisionConflictError,
    ProjectCatalogueState,
    ProjectCatalogueStateConflictError,
    ProjectCatalogueStorageError,
    ProjectCatalogueUnavailableError,
)
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProjectStatus,
)
from impodo.domain.shared.tenancy import OrganizationId, ProjectVisibilityScope

POSTGRESQL_CATALOGUE_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_CATALOGUE_LOCK_TIMEOUT_MILLISECONDS = 1_000
POSTGRESQL_CATALOGUE_STATEMENT_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_CATALOGUE_LIST_CONNECTION_BUDGET = 1
POSTGRESQL_CATALOGUE_LIST_QUERY_BUDGET = 1

_RECORD_COLUMNS = (
    "organization_id, project_id, display_name, status, "
    "data_classification, retention_days, optimistic_revision, "
    "catalogue_state, artifact_root_key, current_receipt_hash, "
    "created_at, updated_at, deletion_requested_at, deleted_at"
)
_CREATE_QUERY: LiteralString = (
    "INSERT INTO impodo_control.project_catalogue (\n"
    "    organization_id, project_id, display_name, status,\n"
    "    data_classification, retention_days, optimistic_revision,\n"
    "    catalogue_state, artifact_root_key, current_receipt_hash,\n"
    "    created_at, updated_at, deletion_requested_at, deleted_at\n"
    ") VALUES (\n"
    "    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s\n"
    ")\n"
    f"RETURNING {_RECORD_COLUMNS}"
)
_GET_QUERY: LiteralString = (
    f"SELECT {_RECORD_COLUMNS}\n"
    "FROM impodo_control.project_catalogue\n"
    "WHERE organization_id = %s AND project_id = %s"
)
_LIST_QUERY: LiteralString = (
    f"SELECT {_RECORD_COLUMNS}\n"
    "FROM impodo_control.project_catalogue\n"
    "WHERE organization_id = %s\n"
    "  AND (%s OR project_id = ANY(%s::uuid[]))\n"
    "  AND (%s::varchar[] IS NULL OR status = ANY(%s::varchar[]))\n"
    "  AND catalogue_state = ANY(%s::varchar[])\n"
    "  AND (\n"
    "      %s::timestamptz IS NULL\n"
    "      OR updated_at < %s\n"
    "      OR (updated_at = %s AND project_id > %s)\n"
    "  )\n"
    "ORDER BY updated_at DESC, project_id ASC\n"
    "LIMIT %s"
)
_RENAME_QUERY: LiteralString = (
    "UPDATE impodo_control.project_catalogue\n"
    "SET display_name = %s,\n"
    "    optimistic_revision = optimistic_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE organization_id = %s\n"
    "  AND project_id = %s\n"
    "  AND optimistic_revision = %s\n"
    "  AND catalogue_state NOT IN ('DELETING', 'DELETED')\n"
    f"RETURNING {_RECORD_COLUMNS}"
)
_UPDATE_LIFECYCLE_QUERY: LiteralString = (
    "UPDATE impodo_control.project_catalogue\n"
    "SET status = %s, catalogue_state = %s,\n"
    "    artifact_root_key = %s, current_receipt_hash = %s,\n"
    "    optimistic_revision = optimistic_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE organization_id = %s\n"
    "  AND project_id = %s\n"
    "  AND optimistic_revision = %s\n"
    "  AND catalogue_state NOT IN ('DELETING', 'DELETED')\n"
    f"RETURNING {_RECORD_COLUMNS}"
)
_START_DELETION_QUERY: LiteralString = (
    "UPDATE impodo_control.project_catalogue\n"
    "SET catalogue_state = 'DELETING', deletion_requested_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE organization_id = %s\n"
    "  AND project_id = %s\n"
    "  AND optimistic_revision = %s\n"
    "  AND catalogue_state IN ('PENDING', 'READY', 'REPAIR_REQUIRED')\n"
    f"RETURNING {_RECORD_COLUMNS}"
)
_FINISH_DELETION_QUERY: LiteralString = (
    "UPDATE impodo_control.project_catalogue\n"
    "SET catalogue_state = 'DELETED', deleted_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE organization_id = %s\n"
    "  AND project_id = %s\n"
    "  AND optimistic_revision = %s\n"
    "  AND catalogue_state = 'DELETING'\n"
    f"RETURNING {_RECORD_COLUMNS}"
)
_READ_MUTATION_STATE_QUERY: LiteralString = (
    "SELECT optimistic_revision, catalogue_state\n"
    "FROM impodo_control.project_catalogue\n"
    "WHERE organization_id = %s AND project_id = %s"
)


class PostgreSQLProjectCatalogueSession(Protocol):
    """Expose one short-lived transaction used by one repository call."""

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None: ...

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgreSQLProjectCatalogueSessionFactory(Protocol):
    """Open one runtime-role catalogue transaction without retaining a pool."""

    def __call__(self, dsn: str) -> PostgreSQLProjectCatalogueSession: ...


class _PsycopgProjectCatalogueSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        row = self._connection.execute(query, parameters).fetchone()
        return tuple(row) if row is not None else None

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        rows = self._connection.execute(query, parameters).fetchall()
        return tuple(tuple(row) for row in rows)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class _MeasuredProjectCatalogueSession:
    def __init__(self, session: PostgreSQLProjectCatalogueSession) -> None:
        self._session = session
        self.query_count = 0

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        self.query_count += 1
        return self._session.fetch_one(query, parameters)

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        self.query_count += 1
        return self._session.fetch_rows(query, parameters)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        self._session.close()


def open_psycopg_project_catalogue_session(
    dsn: str,
) -> PostgreSQLProjectCatalogueSession:
    """Open one bounded runtime-role transaction for one repository call."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_CATALOGUE_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_CATALOGUE_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=5000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_CATALOGUE_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-project-catalogue",
        options=options,
    )
    return _PsycopgProjectCatalogueSession(connection)


_Result = TypeVar("_Result")


class PsycopgProjectCatalogueRepository:
    """Persist control-plane Project facts through bounded Psycopg sessions."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLProjectCatalogueSessionFactory = (
            open_psycopg_project_catalogue_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def create(self, record: ProjectCatalogueRecord) -> ProjectCatalogueRecord:
        """Insert one revision-one pending record without retaining a session."""

        if (
            record.optimistic_revision != 1
            or record.catalogue_state is not ProjectCatalogueState.PENDING
            or record.artifact_root_key is not None
            or record.current_receipt_hash is not None
            or record.deletion_requested_at is not None
            or record.deleted_at is not None
        ):
            raise MigrationFoundationError(
                "new Project catalogue record must start pending at revision one"
            )

        def insert(
            session: PostgreSQLProjectCatalogueSession,
        ) -> ProjectCatalogueRecord:
            row = session.fetch_one(_CREATE_QUERY, _record_values(record))
            if row is None:
                raise ProjectCatalogueStorageError("Project catalogue create failed")
            return _record_from_row(row)

        return self._run("project_catalogue.create", insert)

    def get(
        self,
        organization_id: OrganizationId,
        project_id: str,
    ) -> ProjectCatalogueRecord:
        """Read with Organization and Project identity in the same predicate."""

        project_id = require_uuid(project_id, "project_id")

        def read(session: PostgreSQLProjectCatalogueSession) -> ProjectCatalogueRecord:
            row = session.fetch_one(
                _GET_QUERY,
                (str(organization_id), project_id),
            )
            if row is None:
                raise ProjectCatalogueNotFoundError("Project not found")
            return _record_from_row(row)

        return self._run("project_catalogue.get", read)

    def list_visible(
        self,
        visibility: ProjectVisibilityScope,
        query: ProjectCatalogueListQuery,
    ) -> ProjectCataloguePage:
        """Apply visibility and keyset pagination inside the catalogue query."""

        visible_ids = tuple(
            sorted(require_uuid(item, "project_id") for item in visibility.project_ids)
        )
        if not visibility.organization_wide and not visible_ids:
            return ProjectCataloguePage(records=(), next_cursor=None)
        cursor = (
            ProjectCatalogueCursor.decode(query.cursor)
            if query.cursor is not None
            else None
        )
        statuses = (
            [status.value for status in sorted(query.statuses, key=str)]
            if query.statuses is not None
            else None
        )
        states = [state.value for state in sorted(query.catalogue_states, key=str)]
        cursor_time = cursor.updated_at if cursor is not None else None
        cursor_project_id = cursor.project_id if cursor is not None else None
        parameters: tuple[object, ...] = (
            str(visibility.organization_id),
            visibility.organization_wide,
            list(visible_ids),
            statuses,
            statuses,
            states,
            cursor_time,
            cursor_time,
            cursor_time,
            cursor_project_id,
            query.page_size + 1,
        )

        def read(session: PostgreSQLProjectCatalogueSession) -> ProjectCataloguePage:
            rows = session.fetch_rows(_LIST_QUERY, parameters)
            records = tuple(_record_from_row(row) for row in rows[: query.page_size])
            next_cursor = None
            if len(rows) > query.page_size and records:
                last = records[-1]
                next_cursor = ProjectCatalogueCursor(
                    updated_at=last.updated_at,
                    project_id=last.project_id,
                ).encode()
            return ProjectCataloguePage(records=records, next_cursor=next_cursor)

        return self._run("project_catalogue.list_visible", read)

    def rename(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
        display_name: str,
        updated_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Rename one exact revision while deletion is not in progress."""

        project_id = require_uuid(project_id, "project_id")
        expected_revision = require_revision(expected_revision)
        display_name = required_text(display_name, "display_name", maximum=200)
        require_aware(updated_at, "updated_at")
        parameters = (
            display_name,
            updated_at,
            str(organization_id),
            project_id,
            expected_revision,
        )
        return self._mutate(
            organization_id,
            project_id,
            operation_name="project_catalogue.rename",
            expected_revision=expected_revision,
            query=_RENAME_QUERY,
            parameters=parameters,
        )

    def update_lifecycle_summary(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
        status: MigrationProjectStatus,
        catalogue_state: ProjectCatalogueState,
        artifact_root_key: str | None,
        current_receipt_hash: str | None,
        updated_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Store one verified bounded lifecycle projection atomically."""

        project_id = require_uuid(project_id, "project_id")
        expected_revision = require_revision(expected_revision)
        status = MigrationProjectStatus(status)
        catalogue_state = ProjectCatalogueState(catalogue_state)
        if catalogue_state in {
            ProjectCatalogueState.DELETING,
            ProjectCatalogueState.DELETED,
        }:
            raise MigrationFoundationError(
                "deletion state requires the deletion transition"
            )
        artifact_root_key, current_receipt_hash = _validate_projection_pointer(
            catalogue_state,
            artifact_root_key,
            current_receipt_hash,
        )
        require_aware(updated_at, "updated_at")
        parameters = (
            status.value,
            catalogue_state.value,
            artifact_root_key,
            current_receipt_hash,
            updated_at,
            str(organization_id),
            project_id,
            expected_revision,
        )
        return self._mutate(
            organization_id,
            project_id,
            operation_name="project_catalogue.update_lifecycle_summary",
            expected_revision=expected_revision,
            query=_UPDATE_LIFECYCLE_QUERY,
            parameters=parameters,
        )

    def transition_deletion_state(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
        target_state: ProjectCatalogueState,
        occurred_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Advance the soft-deletion state without exposing a DELETE command."""

        project_id = require_uuid(project_id, "project_id")
        expected_revision = require_revision(expected_revision)
        target_state = ProjectCatalogueState(target_state)
        require_aware(occurred_at, "occurred_at")
        if target_state is ProjectCatalogueState.DELETING:
            query = _START_DELETION_QUERY
        elif target_state is ProjectCatalogueState.DELETED:
            query = _FINISH_DELETION_QUERY
        else:
            raise MigrationFoundationError("deletion target state is invalid")
        parameters = (
            occurred_at,
            occurred_at,
            str(organization_id),
            project_id,
            expected_revision,
        )
        return self._mutate(
            organization_id,
            project_id,
            operation_name="project_catalogue.transition_deletion_state",
            expected_revision=expected_revision,
            query=query,
            parameters=parameters,
        )

    def _mutate(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        operation_name: str,
        expected_revision: int,
        query: LiteralString,
        parameters: tuple[object, ...],
    ) -> ProjectCatalogueRecord:
        def update(
            session: PostgreSQLProjectCatalogueSession,
        ) -> ProjectCatalogueRecord:
            row = session.fetch_one(query, parameters)
            if row is None:
                self._raise_mutation_miss(
                    session,
                    organization_id,
                    project_id,
                    expected_revision=expected_revision,
                )
            if row is None:
                raise ProjectCatalogueStorageError("Project catalogue mutation failed")
            return _record_from_row(row)

        return self._run(operation_name, update)

    @staticmethod
    def _raise_mutation_miss(
        session: PostgreSQLProjectCatalogueSession,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
    ) -> None:
        current = session.fetch_one(
            _READ_MUTATION_STATE_QUERY,
            (str(organization_id), project_id),
        )
        if current is None:
            raise ProjectCatalogueNotFoundError("Project not found")
        try:
            current_revision = _integer_value(current[0])
        except (IndexError, TypeError, ValueError) as error:
            raise ProjectCatalogueStorageError(
                "Project catalogue mutation failed"
            ) from error
        if current_revision != expected_revision:
            raise ProjectCatalogueRevisionConflictError("Project revision is stale")
        raise ProjectCatalogueStateConflictError(
            "Project lifecycle transition is unavailable"
        )

    def _run(
        self,
        operation_name: str,
        operation: Callable[[PostgreSQLProjectCatalogueSession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise ProjectCatalogueUnavailableError(
                "Project catalogue is unavailable"
            ) from error

        measured_session = _MeasuredProjectCatalogueSession(session)
        transaction_started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured_session)
                measured_session.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except ProjectCatalogueError:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise
            except psycopg.errors.UniqueViolation as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise ProjectCatalogueAlreadyExistsError(
                    "Project already exists"
                ) from error
            except (
                psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable,
                psycopg.errors.QueryCanceled,
                psycopg.errors.SerializationFailure,
            ) as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise ProjectCatalogueUnavailableError(
                    "Project catalogue is temporarily unavailable"
                ) from error
            except (psycopg.InterfaceError, psycopg.OperationalError) as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise ProjectCatalogueUnavailableError(
                    "Project catalogue is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise ProjectCatalogueStorageError(
                    "Project catalogue statement failed"
                ) from error
        finally:
            active_error = sys.exception()
            transaction_duration_milliseconds = (
                perf_counter() - transaction_started
            ) * 1000
            try:
                measured_session.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise ProjectCatalogueUnavailableError(
                        "Project catalogue is unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation=operation_name,
                        outcome=outcome,
                        connection_count=1,
                        query_count=measured_session.query_count,
                        pool_wait_milliseconds=0.0,
                        transaction_duration_milliseconds=(
                            transaction_duration_milliseconds
                        ),
                        retry_count=0,
                    ),
                )


def _record_values(record: ProjectCatalogueRecord) -> tuple[object, ...]:
    return (
        str(record.organization_id),
        record.project_id,
        record.display_name,
        record.status.value,
        record.data_classification.value,
        record.retention_days,
        record.optimistic_revision,
        record.catalogue_state.value,
        record.artifact_root_key,
        record.current_receipt_hash,
        record.created_at,
        record.updated_at,
        record.deletion_requested_at,
        record.deleted_at,
    )


def _rollback_or_unavailable(session: PostgreSQLProjectCatalogueSession) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise ProjectCatalogueUnavailableError(
            "Project catalogue is unavailable"
        ) from error


def _record_from_row(row: tuple[object, ...]) -> ProjectCatalogueRecord:
    try:
        return ProjectCatalogueRecord(
            organization_id=OrganizationId(str(row[0])),
            project_id=str(row[1]),
            display_name=str(row[2]),
            status=MigrationProjectStatus(str(row[3])),
            data_classification=MigrationDataClassification(str(row[4])),
            retention_days=_integer_value(row[5]),
            optimistic_revision=_integer_value(row[6]),
            catalogue_state=ProjectCatalogueState(str(row[7])),
            artifact_root_key=(str(row[8]) if row[8] is not None else None),
            current_receipt_hash=(str(row[9]) if row[9] is not None else None),
            created_at=_datetime_value(row[10]),
            updated_at=_datetime_value(row[11]),
            deletion_requested_at=_optional_datetime_value(row[12]),
            deleted_at=_optional_datetime_value(row[13]),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise ProjectCatalogueStorageError(
            "Project catalogue returned an invalid record"
        ) from error


def _datetime_value(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("stored timestamp is invalid")
    require_aware(value, "stored timestamp")
    return value


def _integer_value(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("stored integer is invalid")
    return value


def _optional_datetime_value(value: object) -> datetime | None:
    return None if value is None else _datetime_value(value)


def _validate_projection_pointer(
    state: ProjectCatalogueState,
    artifact_root_key: str | None,
    current_receipt_hash: str | None,
) -> tuple[str | None, str | None]:
    if artifact_root_key is not None:
        artifact_root_key = required_text(
            artifact_root_key,
            "artifact_root_key",
            maximum=500,
        )
        path = PurePosixPath(artifact_root_key)
        if path.is_absolute() or ".." in path.parts or "\\" in artifact_root_key:
            raise MigrationFoundationError("artifact_root_key is invalid")
    if current_receipt_hash is not None:
        require_hash(current_receipt_hash, "current_receipt_hash")
    if state is ProjectCatalogueState.READY and (
        artifact_root_key is None or current_receipt_hash is None
    ):
        raise MigrationFoundationError(
            "ready Project catalogue requires a proven artifact pointer"
        )
    return artifact_root_key, current_receipt_hash


__all__ = [
    "POSTGRESQL_CATALOGUE_CONNECT_TIMEOUT_SECONDS",
    "POSTGRESQL_CATALOGUE_LIST_CONNECTION_BUDGET",
    "POSTGRESQL_CATALOGUE_LIST_QUERY_BUDGET",
    "POSTGRESQL_CATALOGUE_LOCK_TIMEOUT_MILLISECONDS",
    "POSTGRESQL_CATALOGUE_STATEMENT_TIMEOUT_MILLISECONDS",
    "PostgreSQLProjectCatalogueSession",
    "PostgreSQLProjectCatalogueSessionFactory",
    "PsycopgProjectCatalogueRepository",
    "open_psycopg_project_catalogue_session",
]
