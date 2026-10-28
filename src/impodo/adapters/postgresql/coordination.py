"""Persist idempotent operations, immutable receipts, and fenced leases."""

from __future__ import annotations

import base64
import binascii
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any, LiteralString, Protocol, TypeVar
from uuid import uuid4

import psycopg

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.startup import PostgreSQLDsnError, PostgreSQLDsnReader
from impodo.adapters.postgresql.transaction_metrics import (
    PostgreSQLTransactionMetric,
    PostgreSQLTransactionMetricObserver,
    PostgreSQLTransactionOutcome,
    report_postgresql_transaction_metric,
)
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_uuid,
)
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.coordination import (
    ArtifactIdentity,
    ControlPlaneProjectionReceipt,
    CrossStoreOperationSnapshot,
    CrossStoreOperationState,
    DataPlaneOperationReceipt,
    IdempotencyConflictError,
    OperationNotFoundError,
    OperationRequest,
    OperationReservation,
    OperationStateConflictError,
    ReceiptMismatchError,
    RecoveryDisposition,
    StaleLeaseError,
    WorkspaceLease,
)
from impodo.domain.shared.projection_reconciliation import (
    ProjectionReconciliationCursorError,
    ProjectionReconciliationDisposition,
    ProjectionReconciliationItem,
    ProjectionReconciliationPage,
    ProjectionReconciliationQuery,
)
from impodo.domain.shared.tenancy import OrganizationId

POSTGRESQL_COORDINATION_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_COORDINATION_LOCK_TIMEOUT_MILLISECONDS = 1_000
POSTGRESQL_COORDINATION_STATEMENT_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_RECEIPT_MAXIMUM_ARTIFACTS = 1_000
POSTGRESQL_RECEIPT_MAXIMUM_ARTIFACT_JSON_BYTES = 1_000_000
POSTGRESQL_RECONCILIATION_LIST_CONNECTION_BUDGET = 1
POSTGRESQL_RECONCILIATION_LIST_QUERY_BUDGET = 1

_OPERATION_COLUMNS = (
    "operation_id, organization_id, project_id, operation_kind, "
    "idempotency_key, semantic_input_hash, authorization_receipt_id, "
    "requested_by_issuer, requested_by_subject, requested_by_display_name, "
    "requested_at"
)
_RESERVE_OPERATION_QUERY: LiteralString = (
    "INSERT INTO impodo_control.operation_request (\n"
    "    organization_id, operation_id, project_id, operation_kind,\n"
    "    idempotency_key, semantic_input_hash, authorization_receipt_id,\n"
    "    requested_by_issuer, requested_by_subject,\n"
    "    requested_by_display_name, requested_at, updated_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n"
    "ON CONFLICT (organization_id, idempotency_key) DO NOTHING\n"
    f"RETURNING {_OPERATION_COLUMNS}"
)
_READ_OPERATION_BY_KEY_QUERY: LiteralString = (
    f"SELECT {_OPERATION_COLUMNS}\n"
    "FROM impodo_control.operation_request\n"
    "WHERE organization_id = %s AND idempotency_key = %s"
)
_READ_OPERATION_BY_ID_QUERY: LiteralString = (
    f"SELECT {_OPERATION_COLUMNS}, state, data_plane_receipt_hash,\n"
    "       interrupted_at, recovery_disposition\n"
    "FROM impodo_control.operation_request\n"
    "WHERE organization_id = %s AND operation_id = %s"
)
_LOCK_OPERATION_QUERY: LiteralString = (
    "SELECT project_id, operation_kind, semantic_input_hash, state,\n"
    "       data_plane_receipt_hash\n"
    "FROM impodo_control.operation_request\n"
    "WHERE organization_id = %s AND operation_id = %s\n"
    "FOR UPDATE"
)
_LOCK_OPERATION_RECOVERY_QUERY: LiteralString = (
    "SELECT project_id, operation_kind, semantic_input_hash, state,\n"
    "       data_plane_receipt_hash, interrupted_at, recovery_disposition\n"
    "FROM impodo_control.operation_request\n"
    "WHERE organization_id = %s AND operation_id = %s\n"
    "FOR UPDATE"
)
_INSERT_DATA_RECEIPT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.data_plane_receipt (\n"
    "    organization_id, operation_id, receipt_id, project_id, workspace_id,\n"
    "    operation_kind, semantic_input_hash, fencing_token, evidence_hash,\n"
    "    receipt_hash, artifacts, committed_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)\n"
    "RETURNING receipt_hash"
)
_ADVANCE_DATA_COMMITTED_QUERY: LiteralString = (
    "UPDATE impodo_control.operation_request\n"
    "SET state = 'DATA_COMMITTED', data_plane_receipt_hash = %s,\n"
    "    updated_at = %s\n"
    "WHERE organization_id = %s AND operation_id = %s\n"
    "  AND state = 'RESERVED' AND data_plane_receipt_hash IS NULL\n"
    "RETURNING operation_id"
)
_READ_DATA_RECEIPT_QUERY: LiteralString = (
    "SELECT receipt_id, operation_id, project_id, workspace_id,\n"
    "       operation_kind, semantic_input_hash, fencing_token, evidence_hash,\n"
    "       receipt_hash, artifacts, committed_at\n"
    "FROM impodo_control.data_plane_receipt\n"
    "WHERE organization_id = %s AND operation_id = %s"
)
_READ_PROJECTION_RECEIPT_QUERY: LiteralString = (
    "SELECT projection_receipt_id, operation_id, data_plane_receipt_hash,\n"
    "       projection_hash, projection_revision, projected_at\n"
    "FROM impodo_control.projection_receipt\n"
    "WHERE organization_id = %s AND operation_id = %s"
)
_INSERT_PROJECTION_RECEIPT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.projection_receipt (\n"
    "    organization_id, operation_id, projection_receipt_id,\n"
    "    data_plane_receipt_hash, projection_hash, projection_revision,\n"
    "    projected_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s)\n"
    "RETURNING projection_receipt_id"
)
_ADVANCE_PROJECTED_QUERY: LiteralString = (
    "UPDATE impodo_control.operation_request\n"
    "SET state = 'PROJECTED', updated_at = %s\n"
    "WHERE organization_id = %s AND operation_id = %s\n"
    "  AND state = 'DATA_COMMITTED' AND data_plane_receipt_hash = %s\n"
    "RETURNING operation_id"
)
_ADVANCE_PROJECTION_CURRENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.projection_current (\n"
    "    organization_id, project_id, projection_kind, operation_id,\n"
    "    data_plane_receipt_hash, projection_hash, projection_revision,\n"
    "    source_committed_at, projected_at, updated_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n"
    "ON CONFLICT (organization_id, project_id, projection_kind) DO UPDATE\n"
    "SET operation_id = EXCLUDED.operation_id,\n"
    "    data_plane_receipt_hash = EXCLUDED.data_plane_receipt_hash,\n"
    "    projection_hash = EXCLUDED.projection_hash,\n"
    "    projection_revision = EXCLUDED.projection_revision,\n"
    "    source_committed_at = EXCLUDED.source_committed_at,\n"
    "    projected_at = EXCLUDED.projected_at,\n"
    "    updated_at = EXCLUDED.updated_at\n"
    "WHERE (impodo_control.projection_current.source_committed_at,\n"
    "       impodo_control.projection_current.operation_id)\n"
    "    < (EXCLUDED.source_committed_at, EXCLUDED.operation_id)\n"
    "RETURNING operation_id"
)
_READ_PROJECTION_CURRENT_QUERY: LiteralString = (
    "SELECT operation_id, data_plane_receipt_hash, projection_hash,\n"
    "       projection_revision, source_committed_at, projected_at\n"
    "FROM impodo_control.projection_current\n"
    "WHERE organization_id = %s AND project_id = %s AND projection_kind = %s"
)
_RECONCILIATION_SELECT: LiteralString = (
    "SELECT o.operation_id, o.organization_id, o.project_id,\n"
    "       o.operation_kind, o.idempotency_key, o.semantic_input_hash,\n"
    "       o.authorization_receipt_id, o.requested_by_issuer,\n"
    "       o.requested_by_subject, o.requested_by_display_name,\n"
    "       o.requested_at, o.state, o.data_plane_receipt_hash,\n"
    "       o.interrupted_at, o.recovery_disposition,\n"
    "       d.receipt_id, d.operation_id, d.project_id, d.workspace_id,\n"
    "       d.operation_kind, d.semantic_input_hash, d.fencing_token,\n"
    "       d.evidence_hash, d.receipt_hash, d.artifacts, d.committed_at,\n"
    "       p.projection_receipt_id, p.operation_id,\n"
    "       p.data_plane_receipt_hash, p.projection_hash,\n"
    "       p.projection_revision, p.projected_at,\n"
    "       pc.operation_id, pc.data_plane_receipt_hash,\n"
    "       pc.projection_hash, pc.projection_revision,\n"
    "       pc.source_committed_at\n"
    "FROM impodo_control.operation_request AS o\n"
    "LEFT JOIN impodo_control.data_plane_receipt AS d\n"
    "  ON d.organization_id = o.organization_id\n"
    " AND d.operation_id = o.operation_id\n"
    "LEFT JOIN impodo_control.projection_receipt AS p\n"
    "  ON p.organization_id = o.organization_id\n"
    " AND p.operation_id = o.operation_id\n"
    "LEFT JOIN impodo_control.projection_current AS pc\n"
    "  ON pc.organization_id = o.organization_id\n"
    " AND pc.project_id = o.project_id\n"
    " AND pc.projection_kind = o.operation_kind\n"
)
_READ_RECONCILIATION_OPERATION_QUERY: LiteralString = (
    f"{_RECONCILIATION_SELECT}"
    "WHERE o.organization_id = %s AND o.project_id = %s\n"
    "  AND o.operation_id = %s"
)
_LIST_RECONCILIATION_FIRST_PAGE_QUERY: LiteralString = (
    f"{_RECONCILIATION_SELECT}"
    "WHERE o.organization_id = %s AND o.project_id = %s\n"
    "ORDER BY o.requested_at, o.operation_id\n"
    "LIMIT %s"
)
_LIST_RECONCILIATION_AFTER_QUERY: LiteralString = (
    f"{_RECONCILIATION_SELECT}"
    "WHERE o.organization_id = %s AND o.project_id = %s\n"
    "  AND (o.requested_at, o.operation_id) > (%s, %s)\n"
    "ORDER BY o.requested_at, o.operation_id\n"
    "LIMIT %s"
)
_RECORD_RECOVERY_QUERY: LiteralString = (
    "UPDATE impodo_control.operation_request\n"
    "SET state = %s, interrupted_at = %s, recovery_disposition = %s,\n"
    "    updated_at = %s\n"
    "WHERE organization_id = %s AND operation_id = %s AND state = %s\n"
    "  AND data_plane_receipt_hash IS NOT DISTINCT FROM %s\n"
    "RETURNING operation_id"
)
_LEASE_COLUMNS = (
    "lease_id, organization_id, project_id, workspace_id, holder_attempt_id, "
    "fencing_token, acquired_at, heartbeat_at, expires_at, version, released_at"
)
_ACQUIRE_LEASE_QUERY: LiteralString = (
    "INSERT INTO impodo_control.workspace_lease (\n"
    "    organization_id, project_id, workspace_id, lease_id,\n"
    "    holder_attempt_id, fencing_token, acquired_at, heartbeat_at,\n"
    "    expires_at, version, released_at\n"
    ") VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, 1, NULL)\n"
    "ON CONFLICT (organization_id, project_id, workspace_id) DO UPDATE\n"
    "SET lease_id = EXCLUDED.lease_id,\n"
    "    holder_attempt_id = EXCLUDED.holder_attempt_id,\n"
    "    fencing_token = impodo_control.workspace_lease.fencing_token + 1,\n"
    "    acquired_at = EXCLUDED.acquired_at,\n"
    "    heartbeat_at = EXCLUDED.heartbeat_at,\n"
    "    expires_at = EXCLUDED.expires_at,\n"
    "    version = impodo_control.workspace_lease.version + 1,\n"
    "    released_at = NULL\n"
    "WHERE impodo_control.workspace_lease.released_at IS NOT NULL\n"
    "   OR impodo_control.workspace_lease.expires_at <= EXCLUDED.acquired_at\n"
    f"RETURNING {_LEASE_COLUMNS}"
)
_HEARTBEAT_LEASE_QUERY: LiteralString = (
    "UPDATE impodo_control.workspace_lease\n"
    "SET heartbeat_at = %s, expires_at = %s, version = version + 1\n"
    "WHERE organization_id = %s AND project_id = %s AND workspace_id = %s\n"
    "  AND lease_id = %s AND holder_attempt_id = %s\n"
    "  AND fencing_token = %s AND version = %s\n"
    "  AND released_at IS NULL AND expires_at > %s\n"
    "  AND heartbeat_at <= %s AND expires_at < %s\n"
    f"RETURNING {_LEASE_COLUMNS}"
)
_RELEASE_LEASE_QUERY: LiteralString = (
    "UPDATE impodo_control.workspace_lease\n"
    "SET released_at = %s, version = version + 1\n"
    "WHERE organization_id = %s AND project_id = %s AND workspace_id = %s\n"
    "  AND lease_id = %s AND holder_attempt_id = %s\n"
    "  AND fencing_token = %s AND version = %s\n"
    "  AND released_at IS NULL AND expires_at > %s AND heartbeat_at <= %s\n"
    f"RETURNING {_LEASE_COLUMNS}"
)


class PostgreSQLCoordinationUnavailableError(RuntimeError):
    """Report unavailable coordination transport without connection detail."""


class PostgreSQLCoordinationStorageError(RuntimeError):
    """Report rejected or malformed coordination storage without row detail."""


class PostgreSQLCoordinationSession(Protocol):
    """Expose one short-lived coordination transaction."""

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None: ...

    def fetch_all(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgreSQLCoordinationSessionFactory(Protocol):
    """Open one transaction without retaining a connection pool."""

    def __call__(self, dsn: str) -> PostgreSQLCoordinationSession: ...


class _PsycopgCoordinationSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        row = self._connection.execute(query, parameters).fetchone()
        return tuple(row) if row is not None else None

    def fetch_all(
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


class _MeasuredCoordinationSession:
    def __init__(self, session: PostgreSQLCoordinationSession) -> None:
        self._session = session
        self.query_count = 0

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        self.query_count += 1
        return self._session.fetch_one(query, parameters)

    def fetch_all(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        self.query_count += 1
        return self._session.fetch_all(query, parameters)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        self._session.close()


def open_psycopg_coordination_session(dsn: str) -> PostgreSQLCoordinationSession:
    """Open one bounded runtime-role coordination transaction."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_COORDINATION_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_COORDINATION_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=5000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_COORDINATION_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-coordination",
        options=options,
    )
    return _PsycopgCoordinationSession(connection)


_Result = TypeVar("_Result")


class _PsycopgCoordinationRepository:
    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLCoordinationSessionFactory,
        metrics_observer: PostgreSQLTransactionMetricObserver | None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def _run(
        self,
        operation_name: str,
        operation: Callable[[PostgreSQLCoordinationSession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise PostgreSQLCoordinationUnavailableError(
                "Coordination storage is unavailable"
            ) from error

        measured_session = _MeasuredCoordinationSession(session)
        transaction_started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured_session)
                measured_session.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except (
                IdempotencyConflictError,
                OperationNotFoundError,
                OperationStateConflictError,
                PostgreSQLCoordinationStorageError,
                ReceiptMismatchError,
                StaleLeaseError,
            ):
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise
            except psycopg.errors.UniqueViolation as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise IdempotencyConflictError(
                    "Operation or receipt identity already exists"
                ) from error
            except (
                psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable,
                psycopg.errors.QueryCanceled,
                psycopg.errors.SerializationFailure,
            ) as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PostgreSQLCoordinationUnavailableError(
                    "Coordination storage is temporarily unavailable"
                ) from error
            except (psycopg.InterfaceError, psycopg.OperationalError) as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PostgreSQLCoordinationUnavailableError(
                    "Coordination storage is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured_session)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise PostgreSQLCoordinationStorageError(
                    "Coordination statement failed"
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
                    raise PostgreSQLCoordinationUnavailableError(
                        "Coordination storage is unavailable"
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


class PsycopgOperationJournal(_PsycopgCoordinationRepository):
    """Reserve semantic work and retain immutable cross-store receipts."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLCoordinationSessionFactory = (
            open_psycopg_coordination_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        super().__init__(
            dsn_reader,
            dsn_reference,
            session_factory=session_factory,
            metrics_observer=metrics_observer,
        )

    def reserve(self, request: OperationRequest) -> OperationRequest:
        """Create or return one exact Organization-scoped reservation."""

        return self.reserve_with_status(request).request

    def reserve_with_status(
        self,
        request: OperationRequest,
    ) -> OperationReservation:
        """Create or return a reservation and identify an exact replay."""

        operation_id = require_uuid(request.operation_id, "operation_id")
        project_id = require_uuid(request.project_id, "project_id")
        require_hash(request.semantic_input_hash, "semantic_input_hash")
        require_aware(request.requested_at, "requested_at")
        parameters: tuple[object, ...] = (
            str(request.organization_id),
            operation_id,
            project_id,
            request.operation_kind,
            request.idempotency_key,
            request.semantic_input_hash,
            request.authorization_receipt_id,
            request.requested_by.issuer,
            request.requested_by.subject_id,
            request.requested_by.display_name,
            request.requested_at,
            request.requested_at,
        )

        def reserve_operation(
            session: PostgreSQLCoordinationSession,
        ) -> OperationReservation:
            row = session.fetch_one(_RESERVE_OPERATION_QUERY, parameters)
            if row is not None:
                return OperationReservation(request=request, created=True)
            row = session.fetch_one(
                _READ_OPERATION_BY_KEY_QUERY,
                (str(request.organization_id), request.idempotency_key),
            )
            if row is None:
                raise PostgreSQLCoordinationStorageError("Operation reservation failed")
            existing = _operation_from_row(row)
            existing.require_same_idempotent_request(request)
            return OperationReservation(request=existing, created=False)

        return self._run("operation_journal.reserve", reserve_operation)

    def get(self, request: OperationRequest) -> CrossStoreOperationSnapshot:
        """Read exact durable state and immutable receipts for one operation."""

        operation_id = require_uuid(request.operation_id, "operation_id")

        def read_operation(
            session: PostgreSQLCoordinationSession,
        ) -> CrossStoreOperationSnapshot:
            operation_row = session.fetch_one(
                _READ_OPERATION_BY_ID_QUERY,
                (str(request.organization_id), operation_id),
            )
            if operation_row is None:
                raise OperationNotFoundError("Operation not found")
            stored_request = _operation_from_row(operation_row)
            stored_request.require_same_idempotent_request(request)
            if stored_request.operation_id != request.operation_id:
                raise IdempotencyConflictError(
                    "operation identity does not match its reservation"
                )
            data_hash = _optional_text_value(operation_row, 12)
            data_row = (
                session.fetch_one(
                    _READ_DATA_RECEIPT_QUERY,
                    (str(request.organization_id), operation_id),
                )
                if data_hash is not None
                else None
            )
            projection_row = (
                session.fetch_one(
                    _READ_PROJECTION_RECEIPT_QUERY,
                    (str(request.organization_id), operation_id),
                )
                if str(operation_row[11]) == CrossStoreOperationState.PROJECTED
                else None
            )
            return _snapshot_from_rows(
                operation_row,
                data_row=data_row,
                projection_row=projection_row,
            )

        return self._run("operation_journal.get", read_operation)

    def list_reconciliation_page(
        self,
        organization_id: OrganizationId,
        project_id: str,
        query: ProjectionReconciliationQuery,
    ) -> ProjectionReconciliationPage:
        """Read one deterministic page and verify every included receipt."""

        project_id = require_uuid(project_id, "project_id")
        organization_value = str(organization_id)
        if query.cursor is None:
            statement = _LIST_RECONCILIATION_FIRST_PAGE_QUERY
            parameters: tuple[object, ...] = (
                organization_value,
                project_id,
                query.page_size + 1,
            )
        else:
            requested_at, operation_id = _decode_reconciliation_cursor(
                query.cursor,
                organization_id=organization_id,
                project_id=project_id,
            )
            statement = _LIST_RECONCILIATION_AFTER_QUERY
            parameters = (
                organization_value,
                project_id,
                requested_at,
                operation_id,
                query.page_size + 1,
            )

        def read_page(
            session: PostgreSQLCoordinationSession,
        ) -> ProjectionReconciliationPage:
            rows = session.fetch_all(statement, parameters)
            page_rows = rows[: query.page_size]
            items = tuple(_reconciliation_item_from_row(row) for row in page_rows)
            next_cursor = None
            if len(rows) > query.page_size and page_rows:
                last = _operation_from_row(page_rows[-1])
                next_cursor = _encode_reconciliation_cursor(
                    organization_id,
                    project_id,
                    last.requested_at,
                    last.operation_id,
                )
            return ProjectionReconciliationPage(
                items=items,
                next_cursor=next_cursor,
            )

        return self._run("operation_journal.list_reconciliation_page", read_page)

    def get_reconciliation_snapshot(
        self,
        organization_id: OrganizationId,
        project_id: str,
        operation_id: str,
    ) -> CrossStoreOperationSnapshot:
        """Read one exact operation through its Organization and Project scope."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")

        def read_snapshot(
            session: PostgreSQLCoordinationSession,
        ) -> CrossStoreOperationSnapshot:
            row = session.fetch_one(
                _READ_RECONCILIATION_OPERATION_QUERY,
                (str(organization_id), project_id, operation_id),
            )
            if row is None:
                raise OperationNotFoundError("Operation not found")
            snapshot, _current_operation_id = _reconciliation_snapshot_from_row(row)
            return snapshot

        return self._run(
            "operation_journal.get_reconciliation_snapshot",
            read_snapshot,
        )

    def record_data_plane_receipt(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> None:
        """Attach one exact immutable data-plane result to its operation."""

        operation_id = require_uuid(receipt.operation_id, "operation_id")
        project_id = require_uuid(receipt.project_id, "project_id")
        workspace_id = (
            require_uuid(receipt.workspace_id, "workspace_id")
            if receipt.workspace_id is not None
            else None
        )
        artifacts_json = _artifact_json(receipt.artifacts)
        receipt_hash = receipt.semantic_hash

        def record(session: PostgreSQLCoordinationSession) -> None:
            current = session.fetch_one(
                _LOCK_OPERATION_QUERY,
                (str(receipt.organization_id), operation_id),
            )
            if current is None or not _operation_matches_receipt(current, receipt):
                raise ReceiptMismatchError(
                    "data-plane receipt does not match its operation"
                )
            existing_hash = _optional_text_value(current, 4)
            if existing_hash is not None:
                if existing_hash != receipt_hash:
                    raise ReceiptMismatchError(
                        "operation already has different data-plane evidence"
                    )
                return
            if str(current[3]) != "RESERVED":
                raise ReceiptMismatchError(
                    "operation cannot accept data-plane evidence"
                )
            inserted = session.fetch_one(
                _INSERT_DATA_RECEIPT_QUERY,
                (
                    str(receipt.organization_id),
                    operation_id,
                    receipt.receipt_id,
                    project_id,
                    workspace_id,
                    receipt.operation_kind,
                    receipt.semantic_input_hash,
                    receipt.fencing_token,
                    receipt.evidence_hash,
                    receipt_hash,
                    artifacts_json,
                    receipt.committed_at,
                ),
            )
            if inserted != (receipt_hash,):
                raise PostgreSQLCoordinationStorageError(
                    "Data-plane receipt storage failed"
                )
            advanced = session.fetch_one(
                _ADVANCE_DATA_COMMITTED_QUERY,
                (
                    receipt_hash,
                    receipt.committed_at,
                    str(receipt.organization_id),
                    operation_id,
                ),
            )
            if not _returned_identity_matches(advanced, operation_id):
                raise PostgreSQLCoordinationStorageError(
                    "Operation receipt transition failed"
                )

        self._run("operation_journal.record_data_plane_receipt", record)

    def record_projection_receipt(
        self,
        receipt: ControlPlaneProjectionReceipt,
    ) -> None:
        """Bind a projection to the exact stored data-plane receipt once."""

        operation_id = require_uuid(receipt.operation_id, "operation_id")
        project_id = require_uuid(receipt.project_id, "project_id")

        def record(session: PostgreSQLCoordinationSession) -> None:
            current = session.fetch_one(
                _LOCK_OPERATION_QUERY,
                (str(receipt.organization_id), operation_id),
            )
            if (
                current is None
                or str(current[0]) != project_id
                or _optional_text_value(current, 4) != receipt.data_plane_receipt_hash
            ):
                raise ReceiptMismatchError(
                    "projection does not match stored data-plane evidence"
                )
            data_row = session.fetch_one(
                _READ_DATA_RECEIPT_QUERY,
                (str(receipt.organization_id), operation_id),
            )
            if data_row is None:
                raise ReceiptMismatchError(
                    "projection does not have stored data-plane evidence"
                )
            data_receipt = _data_receipt_from_row(
                data_row,
                organization_id=receipt.organization_id,
            )
            receipt.require_data_plane_receipt(data_receipt)
            existing = session.fetch_one(
                _READ_PROJECTION_RECEIPT_QUERY,
                (str(receipt.organization_id), operation_id),
            )
            if existing is not None:
                if not _projection_row_matches(existing, receipt):
                    raise ReceiptMismatchError(
                        "operation already has a different projection receipt"
                    )
                self._advance_projection_current(
                    session,
                    receipt,
                    projection_kind=str(current[1]),
                    source_committed_at=data_receipt.committed_at,
                )
                return
            if str(current[3]) != "DATA_COMMITTED":
                raise ReceiptMismatchError(
                    "operation cannot accept a projection receipt"
                )
            inserted = session.fetch_one(
                _INSERT_PROJECTION_RECEIPT_QUERY,
                (
                    str(receipt.organization_id),
                    operation_id,
                    receipt.projection_receipt_id,
                    receipt.data_plane_receipt_hash,
                    receipt.projection_hash,
                    receipt.projection_revision,
                    receipt.projected_at,
                ),
            )
            if inserted != (receipt.projection_receipt_id,):
                raise PostgreSQLCoordinationStorageError(
                    "Projection receipt storage failed"
                )
            advanced = session.fetch_one(
                _ADVANCE_PROJECTED_QUERY,
                (
                    receipt.projected_at,
                    str(receipt.organization_id),
                    operation_id,
                    receipt.data_plane_receipt_hash,
                ),
            )
            if not _returned_identity_matches(advanced, operation_id):
                raise PostgreSQLCoordinationStorageError(
                    "Operation projection transition failed"
                )
            self._advance_projection_current(
                session,
                receipt,
                projection_kind=str(current[1]),
                source_committed_at=data_receipt.committed_at,
            )

        self._run("operation_journal.record_projection_receipt", record)

    @staticmethod
    def _advance_projection_current(
        session: PostgreSQLCoordinationSession,
        receipt: ControlPlaneProjectionReceipt,
        *,
        projection_kind: str,
        source_committed_at: datetime,
    ) -> None:
        candidate = (
            str(receipt.organization_id),
            receipt.project_id,
            projection_kind,
            receipt.operation_id,
            receipt.data_plane_receipt_hash,
            receipt.projection_hash,
            receipt.projection_revision,
            source_committed_at,
            receipt.projected_at,
            receipt.projected_at,
        )
        advanced = session.fetch_one(_ADVANCE_PROJECTION_CURRENT_QUERY, candidate)
        if _returned_identity_matches(advanced, receipt.operation_id):
            return
        current = session.fetch_one(
            _READ_PROJECTION_CURRENT_QUERY,
            (
                str(receipt.organization_id),
                receipt.project_id,
                projection_kind,
            ),
        )
        if current is None or not _current_projection_accepts_candidate(
            current,
            receipt,
            source_committed_at=source_committed_at,
        ):
            raise PostgreSQLCoordinationStorageError(
                "Current projection pointer did not advance safely"
            )

    def record_interruption(
        self,
        request: OperationRequest,
        *,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        """Record that recovery found no committed data-plane receipt."""

        self._record_recovery_state(
            request,
            state=CrossStoreOperationState.INTERRUPTED,
            interrupted_at=interrupted_at,
            disposition=disposition,
        )

    def require_manual_review(
        self,
        request: OperationRequest,
        *,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        """Stop automatic recovery for ambiguous or substituted evidence."""

        self._record_recovery_state(
            request,
            state=CrossStoreOperationState.MANUAL_REVIEW,
            interrupted_at=interrupted_at,
            disposition=disposition,
        )

    def _record_recovery_state(
        self,
        request: OperationRequest,
        *,
        state: CrossStoreOperationState,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        operation_id = require_uuid(request.operation_id, "operation_id")
        require_aware(interrupted_at, "interrupted_at")

        def record_state(session: PostgreSQLCoordinationSession) -> None:
            row = session.fetch_one(
                _LOCK_OPERATION_RECOVERY_QUERY,
                (str(request.organization_id), operation_id),
            )
            if row is None:
                raise OperationNotFoundError("Operation not found")
            if not _operation_row_matches_request(row, request):
                raise IdempotencyConflictError(
                    "operation identity does not match its reservation"
                )
            current_state = CrossStoreOperationState(str(row[3]))
            current_data_hash = _optional_text_value(row, 4)
            current_interrupted_at = _optional_datetime_value(row, 5)
            current_disposition = _optional_text_value(row, 6)
            if current_state is state:
                if (
                    current_interrupted_at == interrupted_at
                    and current_disposition == disposition
                ):
                    return
                raise OperationStateConflictError(
                    "Operation already has a different recovery disposition"
                )
            if current_state in {
                CrossStoreOperationState.PROJECTED,
                CrossStoreOperationState.INTERRUPTED,
                CrossStoreOperationState.MANUAL_REVIEW,
            }:
                raise OperationStateConflictError(
                    "Operation recovery state is already terminal"
                )
            if state is CrossStoreOperationState.INTERRUPTED and (
                current_state is not CrossStoreOperationState.RESERVED
                or current_data_hash is not None
            ):
                raise OperationStateConflictError(
                    "Committed data evidence cannot become an interruption"
                )
            updated = session.fetch_one(
                _RECORD_RECOVERY_QUERY,
                (
                    state,
                    interrupted_at,
                    disposition,
                    interrupted_at,
                    str(request.organization_id),
                    operation_id,
                    current_state,
                    current_data_hash,
                ),
            )
            if not _returned_identity_matches(updated, operation_id):
                raise OperationStateConflictError(
                    "Operation recovery state changed concurrently"
                )

        self._run("operation_journal.record_recovery_state", record_state)


class PsycopgWorkspaceLeaseRepository(_PsycopgCoordinationRepository):
    """Own one current lease row and monotonic token for each Workspace."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLCoordinationSessionFactory = (
            open_psycopg_coordination_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        super().__init__(
            dsn_reader,
            dsn_reference,
            session_factory=session_factory,
            metrics_observer=metrics_observer,
        )

    def acquire(
        self,
        *,
        organization_id: OrganizationId,
        project_id: str,
        workspace_id: str,
        holder_attempt_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        """Acquire an unowned or expired Workspace with a newer token."""

        project_id = require_uuid(project_id, "project_id")
        workspace_id = require_uuid(workspace_id, "workspace_id")
        holder_attempt_id = require_uuid(holder_attempt_id, "holder_attempt_id")
        require_aware(acquired_at, "acquired_at")
        require_aware(expires_at, "expires_at")
        if expires_at <= acquired_at:
            raise MigrationFoundationError("lease expiry must follow acquisition")
        lease_id = str(uuid4())

        def acquire_lease(
            session: PostgreSQLCoordinationSession,
        ) -> WorkspaceLease:
            row = session.fetch_one(
                _ACQUIRE_LEASE_QUERY,
                (
                    str(organization_id),
                    project_id,
                    workspace_id,
                    lease_id,
                    holder_attempt_id,
                    acquired_at,
                    acquired_at,
                    expires_at,
                ),
            )
            if row is None:
                raise StaleLeaseError("Workspace already has an active lease")
            return _lease_from_row(row)

        return self._run("workspace_lease.acquire", acquire_lease)

    def heartbeat(
        self,
        lease: WorkspaceLease,
        *,
        at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        """Extend only the exact current lease revision and fencing token."""

        candidate = lease.heartbeat(
            fencing_token=lease.fencing_token,
            at=at,
            expires_at=expires_at,
        )

        def heartbeat_lease(
            session: PostgreSQLCoordinationSession,
        ) -> WorkspaceLease:
            row = session.fetch_one(
                _HEARTBEAT_LEASE_QUERY,
                (
                    candidate.heartbeat_at,
                    candidate.expires_at,
                    str(lease.organization_id),
                    require_uuid(lease.project_id, "project_id"),
                    require_uuid(lease.workspace_id, "workspace_id"),
                    require_uuid(lease.lease_id, "lease_id"),
                    require_uuid(lease.holder_attempt_id, "holder_attempt_id"),
                    lease.fencing_token,
                    lease.version,
                    at,
                    at,
                    expires_at,
                ),
            )
            if row is None:
                raise StaleLeaseError("Workspace lease is expired, released, or fenced")
            return _lease_from_row(row)

        return self._run("workspace_lease.heartbeat", heartbeat_lease)

    def release(self, lease: WorkspaceLease, *, at: datetime) -> WorkspaceLease:
        """Release only the exact current lease revision and fencing token."""

        candidate = lease.release(fencing_token=lease.fencing_token, at=at)

        def release_lease(
            session: PostgreSQLCoordinationSession,
        ) -> WorkspaceLease:
            row = session.fetch_one(
                _RELEASE_LEASE_QUERY,
                (
                    candidate.released_at,
                    str(lease.organization_id),
                    require_uuid(lease.project_id, "project_id"),
                    require_uuid(lease.workspace_id, "workspace_id"),
                    require_uuid(lease.lease_id, "lease_id"),
                    require_uuid(lease.holder_attempt_id, "holder_attempt_id"),
                    lease.fencing_token,
                    lease.version,
                    at,
                    at,
                ),
            )
            if row is None:
                raise StaleLeaseError("Workspace lease is expired, released, or fenced")
            return _lease_from_row(row)

        return self._run("workspace_lease.release", release_lease)


def _reconciliation_snapshot_from_row(
    row: tuple[object, ...],
) -> tuple[CrossStoreOperationSnapshot, str | None]:
    try:
        data_row = tuple(row[15:26]) if row[15] is not None else None
        projection_row = tuple(row[26:32]) if row[26] is not None else None
        snapshot = _snapshot_from_rows(
            tuple(row[:15]),
            data_row=data_row,
            projection_row=projection_row,
        )
        current_values = tuple(row[32:37])
        if all(value is None for value in current_values):
            current_operation_id = None
        elif any(value is None for value in current_values):
            raise ValueError("current projection pointer is incomplete")
        else:
            current_operation_id = require_uuid(str(row[32]), "operation_id")
            require_hash(str(row[33]), "data_plane_receipt_hash")
            require_hash(str(row[34]), "projection_hash")
            if _integer_value(row[35]) < 1:
                raise ValueError("projection revision is invalid")
            require_aware(_datetime_value(row[36]), "source_committed_at")

        if snapshot.state is CrossStoreOperationState.PROJECTED:
            data_receipt = snapshot.data_plane_receipt
            projection = snapshot.projection_receipt
            if (
                data_receipt is None
                or projection is None
                or current_operation_id is None
            ):
                raise ValueError("projected operation is missing current evidence")
            if current_operation_id == snapshot.request.operation_id and (
                str(row[33]) != data_receipt.semantic_hash
                or str(row[34]) != projection.projection_hash
                or _integer_value(row[35]) != projection.projection_revision
                or _datetime_value(row[36]) != data_receipt.committed_at
            ):
                raise ValueError("current projection pointer is inconsistent")
        elif current_operation_id == snapshot.request.operation_id:
            raise ValueError("non-projected operation cannot be current")
        return snapshot, current_operation_id
    except (IndexError, MigrationFoundationError, TypeError, ValueError) as error:
        raise PostgreSQLCoordinationStorageError(
            "Projection reconciliation returned invalid receipt evidence"
        ) from error


def _reconciliation_item_from_row(
    row: tuple[object, ...],
) -> ProjectionReconciliationItem:
    snapshot, current_operation_id = _reconciliation_snapshot_from_row(row)
    state = snapshot.state
    if state is CrossStoreOperationState.RESERVED:
        disposition = ProjectionReconciliationDisposition.AWAITING_DATA_COMMIT
    elif state is CrossStoreOperationState.DATA_COMMITTED:
        disposition = ProjectionReconciliationDisposition.MISSING_PROJECTION
    elif state is CrossStoreOperationState.PROJECTED:
        disposition = (
            ProjectionReconciliationDisposition.CURRENT
            if current_operation_id == snapshot.request.operation_id
            else ProjectionReconciliationDisposition.SUPERSEDED
        )
    else:
        disposition = ProjectionReconciliationDisposition.NOT_REBUILDABLE
    data_receipt = snapshot.data_plane_receipt
    projection = snapshot.projection_receipt
    return ProjectionReconciliationItem(
        operation_id=snapshot.request.operation_id,
        operation_kind=snapshot.request.operation_kind,
        requested_at=snapshot.request.requested_at,
        state=state,
        disposition=disposition,
        data_plane_receipt_hash=(
            data_receipt.semantic_hash if data_receipt is not None else None
        ),
        projection_hash=(
            projection.projection_hash if projection is not None else None
        ),
        projection_revision=(
            projection.projection_revision if projection is not None else None
        ),
    )


def _encode_reconciliation_cursor(
    organization_id: OrganizationId,
    project_id: str,
    requested_at: datetime,
    operation_id: str,
) -> str:
    payload = json.dumps(
        {
            "operation_id": operation_id,
            "organization_id": str(organization_id),
            "project_id": project_id,
            "requested_at": requested_at.isoformat(),
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_reconciliation_cursor(
    cursor: str,
    *,
    organization_id: OrganizationId,
    project_id: str,
) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("cursor payload must be an object")
        if (
            payload.get("version") != 1
            or payload.get("organization_id") != str(organization_id)
            or payload.get("project_id") != project_id
        ):
            raise ValueError("cursor scope does not match")
        requested_at_value = payload.get("requested_at")
        operation_id_value = payload.get("operation_id")
        if not isinstance(requested_at_value, str) or not isinstance(
            operation_id_value,
            str,
        ):
            raise TypeError("cursor values are invalid")
        requested_at = datetime.fromisoformat(requested_at_value)
        require_aware(requested_at, "requested_at")
        operation_id = require_uuid(operation_id_value, "operation_id")
        return requested_at, operation_id
    except (
        binascii.Error,
        json.JSONDecodeError,
        MigrationFoundationError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise ProjectionReconciliationCursorError(
            "Projection reconciliation cursor is invalid"
        ) from error


def _current_projection_accepts_candidate(
    row: tuple[object, ...],
    receipt: ControlPlaneProjectionReceipt,
    *,
    source_committed_at: datetime,
) -> bool:
    try:
        current_operation_id = require_uuid(str(row[0]), "operation_id")
        current_source_committed_at = _datetime_value(row[4])
        current_key = (current_source_committed_at, current_operation_id)
        candidate_key = (source_committed_at, receipt.operation_id)
        if current_operation_id != receipt.operation_id:
            return current_key > candidate_key
        return (
            str(row[1]) == receipt.data_plane_receipt_hash
            and str(row[2]) == receipt.projection_hash
            and _integer_value(row[3]) == receipt.projection_revision
            and current_source_committed_at == source_committed_at
            and _datetime_value(row[5]) == receipt.projected_at
        )
    except (IndexError, MigrationFoundationError, TypeError, ValueError):
        return False


def _operation_from_row(row: tuple[object, ...]) -> OperationRequest:
    try:
        return OperationRequest(
            operation_id=str(row[0]),
            organization_id=OrganizationId(str(row[1])),
            project_id=str(row[2]),
            operation_kind=str(row[3]),
            idempotency_key=str(row[4]),
            semantic_input_hash=str(row[5]),
            authorization_receipt_id=str(row[6]),
            requested_by=ActorIdentity(
                issuer=str(row[7]),
                subject_id=str(row[8]),
                display_name=str(row[9]),
            ),
            requested_at=_datetime_value(row[10]),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise PostgreSQLCoordinationStorageError(
            "Operation storage returned an invalid record"
        ) from error


def _snapshot_from_rows(
    operation_row: tuple[object, ...],
    *,
    data_row: tuple[object, ...] | None,
    projection_row: tuple[object, ...] | None,
) -> CrossStoreOperationSnapshot:
    try:
        request = _operation_from_row(operation_row)
        state = CrossStoreOperationState(str(operation_row[11]))
        stored_data_hash = _optional_text_value(operation_row, 12)
        data_receipt = (
            _data_receipt_from_row(
                data_row,
                organization_id=request.organization_id,
            )
            if data_row is not None
            else None
        )
        if (data_receipt is None and stored_data_hash is not None) or (
            data_receipt is not None and data_receipt.semantic_hash != stored_data_hash
        ):
            raise ValueError("stored data-plane receipt hash is inconsistent")
        projection_receipt = (
            _projection_receipt_from_row(
                projection_row,
                organization_id=request.organization_id,
                project_id=request.project_id,
            )
            if projection_row is not None
            else None
        )
        interrupted_at = _optional_datetime_value(operation_row, 13)
        recovery_value = _optional_text_value(operation_row, 14)
        return CrossStoreOperationSnapshot(
            request=request,
            state=state,
            data_plane_receipt=data_receipt,
            projection_receipt=projection_receipt,
            interrupted_at=interrupted_at,
            recovery_disposition=(
                RecoveryDisposition(recovery_value)
                if recovery_value is not None
                else None
            ),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise PostgreSQLCoordinationStorageError(
            "Operation storage returned invalid recovery evidence"
        ) from error


def _data_receipt_from_row(
    row: tuple[object, ...],
    *,
    organization_id: OrganizationId,
) -> DataPlaneOperationReceipt:
    try:
        receipt = DataPlaneOperationReceipt(
            receipt_id=str(row[0]),
            operation_id=str(row[1]),
            organization_id=organization_id,
            project_id=str(row[2]),
            workspace_id=(str(row[3]) if row[3] is not None else None),
            operation_kind=str(row[4]),
            semantic_input_hash=str(row[5]),
            fencing_token=(_integer_value(row[6]) if row[6] is not None else None),
            evidence_hash=str(row[7]),
            artifacts=_artifacts_from_value(row[9]),
            committed_at=_datetime_value(row[10]),
        )
        if receipt.semantic_hash != str(row[8]):
            raise ValueError("data-plane receipt hash is inconsistent")
        return receipt
    except (IndexError, TypeError, ValueError) as error:
        raise PostgreSQLCoordinationStorageError(
            "Data-plane receipt storage returned an invalid record"
        ) from error


def _projection_receipt_from_row(
    row: tuple[object, ...],
    *,
    organization_id: OrganizationId,
    project_id: str,
) -> ControlPlaneProjectionReceipt:
    try:
        return ControlPlaneProjectionReceipt(
            projection_receipt_id=str(row[0]),
            operation_id=str(row[1]),
            organization_id=organization_id,
            project_id=project_id,
            data_plane_receipt_hash=str(row[2]),
            projection_hash=str(row[3]),
            projection_revision=_integer_value(row[4]),
            projected_at=_datetime_value(row[5]),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise PostgreSQLCoordinationStorageError(
            "Projection receipt storage returned an invalid record"
        ) from error


def _artifacts_from_value(value: object) -> tuple[ArtifactIdentity, ...]:
    decoded: object
    if isinstance(value, str):
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, list):
        raise TypeError("stored artifacts must be a JSON array")
    artifacts: list[ArtifactIdentity] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise TypeError("stored artifact must be a JSON object")
        artifacts.append(
            ArtifactIdentity(
                artifact_id=_stored_text(item.get("artifact_id")),
                owner_kind=_stored_text(item.get("owner_kind")),
                owner_id=_stored_text(item.get("owner_id")),
                artifact_kind=_stored_text(item.get("artifact_kind")),
                storage_key=_stored_text(item.get("storage_key")),
                sha256=_stored_text(item.get("sha256")),
                size_bytes=_integer_value(item.get("size_bytes")),
                media_type=_stored_text(item.get("media_type")),
            )
        )
    return tuple(artifacts)


def _stored_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("stored text is invalid")
    return value


def _lease_from_row(row: tuple[object, ...]) -> WorkspaceLease:
    try:
        return WorkspaceLease(
            lease_id=str(row[0]),
            organization_id=OrganizationId(str(row[1])),
            project_id=str(row[2]),
            workspace_id=str(row[3]),
            holder_attempt_id=str(row[4]),
            fencing_token=_integer_value(row[5]),
            acquired_at=_datetime_value(row[6]),
            heartbeat_at=_datetime_value(row[7]),
            expires_at=_datetime_value(row[8]),
            version=_integer_value(row[9]),
            released_at=(_datetime_value(row[10]) if row[10] is not None else None),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise PostgreSQLCoordinationStorageError(
            "Lease storage returned an invalid record"
        ) from error


def _operation_matches_receipt(
    row: tuple[object, ...],
    receipt: DataPlaneOperationReceipt,
) -> bool:
    try:
        return (
            str(row[0]) == receipt.project_id
            and str(row[1]) == receipt.operation_kind
            and str(row[2]) == receipt.semantic_input_hash
        )
    except IndexError:
        return False


def _operation_row_matches_request(
    row: tuple[object, ...],
    request: OperationRequest,
) -> bool:
    try:
        return (
            str(row[0]) == request.project_id
            and str(row[1]) == request.operation_kind
            and str(row[2]) == request.semantic_input_hash
        )
    except IndexError:
        return False


def _artifact_json(artifacts: tuple[ArtifactIdentity, ...]) -> str:
    if len(artifacts) > POSTGRESQL_RECEIPT_MAXIMUM_ARTIFACTS:
        raise MigrationFoundationError("receipt contains too many artifacts")
    payload = [
        {
            "artifact_id": artifact.artifact_id,
            "artifact_kind": artifact.artifact_kind,
            "media_type": artifact.media_type,
            "owner_id": artifact.owner_id,
            "owner_kind": artifact.owner_kind,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "storage_key": artifact.storage_key,
        }
        for artifact in artifacts
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > POSTGRESQL_RECEIPT_MAXIMUM_ARTIFACT_JSON_BYTES:
        raise MigrationFoundationError("receipt artifact projection is too large")
    return encoded


def _optional_text_value(row: tuple[object, ...], index: int) -> str | None:
    try:
        value = row[index]
    except IndexError as error:
        raise PostgreSQLCoordinationStorageError(
            "Coordination storage returned an invalid record"
        ) from error
    return None if value is None else str(value)


def _optional_datetime_value(
    row: tuple[object, ...],
    index: int,
) -> datetime | None:
    try:
        value = row[index]
    except IndexError as error:
        raise PostgreSQLCoordinationStorageError(
            "Coordination storage returned an invalid record"
        ) from error
    return None if value is None else _datetime_value(value)


def _returned_identity_matches(
    row: tuple[object, ...] | None,
    expected: str,
) -> bool:
    return row is not None and len(row) == 1 and str(row[0]) == expected


def _projection_row_matches(
    row: tuple[object, ...],
    receipt: ControlPlaneProjectionReceipt,
) -> bool:
    try:
        return (
            str(row[0]) == receipt.projection_receipt_id
            and str(row[1]) == receipt.operation_id
            and str(row[2]) == receipt.data_plane_receipt_hash
            and str(row[3]) == receipt.projection_hash
            and _integer_value(row[4]) == receipt.projection_revision
            and _datetime_value(row[5]) == receipt.projected_at
        )
    except (IndexError, TypeError, ValueError):
        return False


def _integer_value(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("stored integer is invalid")
    return value


def _datetime_value(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("stored timestamp is invalid")
    require_aware(value, "stored timestamp")
    return value


def _rollback_or_unavailable(session: PostgreSQLCoordinationSession) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise PostgreSQLCoordinationUnavailableError(
            "Coordination storage is unavailable"
        ) from error


__all__ = [
    "POSTGRESQL_COORDINATION_CONNECT_TIMEOUT_SECONDS",
    "POSTGRESQL_COORDINATION_LOCK_TIMEOUT_MILLISECONDS",
    "POSTGRESQL_COORDINATION_STATEMENT_TIMEOUT_MILLISECONDS",
    "POSTGRESQL_RECEIPT_MAXIMUM_ARTIFACTS",
    "POSTGRESQL_RECEIPT_MAXIMUM_ARTIFACT_JSON_BYTES",
    "POSTGRESQL_RECONCILIATION_LIST_CONNECTION_BUDGET",
    "POSTGRESQL_RECONCILIATION_LIST_QUERY_BUDGET",
    "PostgreSQLCoordinationSession",
    "PostgreSQLCoordinationSessionFactory",
    "PostgreSQLCoordinationStorageError",
    "PostgreSQLCoordinationUnavailableError",
    "PsycopgOperationJournal",
    "PsycopgWorkspaceLeaseRepository",
    "open_psycopg_coordination_session",
]
