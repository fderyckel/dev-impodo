"""Persist practical load runs without spanning Odoo network calls."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Sequence
from uuid import UUID

from impodo.domain.shared.access import Actor
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
    MAX_CREATE_BATCH_ROWS,
)
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_preflight import TransferPreflightReport
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class ExecutionRepository(DuckDbRepository):
    """Own the target-specific row journal and current execution pointer."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        super().__init__(database)

    def start_run(
        self,
        workspace_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
        correction_plan_hash: str = "",
        transfer_preflight_hash: str = "",
    ) -> None:
        self._assert_workspace_mutable(workspace_id)
        try:
            canonical_run_id = str(UUID(run.run_id))
            canonical_preflight_id = str(UUID(run.preflight_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Execution run identifier is invalid") from error
        if (
            run.workspace_id != workspace_id
            or run.status is not ExecutionRunStatus.RUNNING
            or run.completed_at is not None
            or run.batch_rows is None
            or run.batch_rows < 1
            or run.batch_rows > MAX_CREATE_BATCH_ROWS
            or not run.rows
            or any(item.status is not ExecutionRowStatus.PLANNED for item in run.rows)
        ):
            raise WorkspaceError("Execution run is invalid")
        correction = bool(correction_plan_hash)
        transfer = bool(transfer_preflight_hash)
        if correction and transfer:
            raise WorkspaceError("Execution run cannot have two authorization modes")
        if correction and (
            _SHA256.fullmatch(correction_plan_hash) is None
            or any(
                item.operation != "UPDATE"
                or item.odoo_id is None
                or item.odoo_id <= 0
                for item in run.rows
            )
        ):
            raise WorkspaceError("Correction execution run is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                if transfer:
                    if _SHA256.fullmatch(transfer_preflight_hash) is None:
                        raise WorkspaceError(
                            "Transfer preflight authorization is invalid"
                        )
                    stored = connection.execute(
                        """
                        SELECT transfer_preflight_report_json
                          FROM workspace_projection_cache
                         WHERE singleton_id = 1
                        """
                    ).fetchone()
                    try:
                        report = (
                            TransferPreflightReport.from_json(str(stored[0]))
                            if stored is not None and stored[0] is not None
                            else None
                        )
                    except ValueError as error:
                        raise WorkspaceError(
                            "The destination preflight evidence is invalid"
                        ) from error
                    if (
                        report is None
                        or not report.ready
                        or report.workspace_id != workspace_id
                        or report.content_hash != transfer_preflight_hash
                        or report.destination_target_hash != run.target_hash
                    ):
                        raise WorkspaceError(
                            "The destination preflight is no longer current"
                        )
                elif not correction:
                    current = connection.execute(
                        """
                        SELECT readiness.run_id, readiness.target_hash
                          FROM preflight_current AS current
                          JOIN readiness_run AS readiness
                            ON readiness.run_id = current.run_id
                         WHERE current.singleton_id = 1
                        """
                    ).fetchone()
                    if current is None or (
                        str(current[0]) != canonical_preflight_id
                        or str(current[1]) != run.target_hash
                    ):
                        raise WorkspaceError(
                            "The load preview is no longer current. "
                            "Compare with Odoo again."
                        )
                previous = connection.execute(
                    """
                    SELECT run.status
                      FROM execution_current AS current
                      JOIN execution_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                if previous is not None and str(previous[0]) == "RUNNING":
                    raise WorkspaceError("Another load is already running")
                connection.execute(
                    """
                    INSERT INTO execution_run (
                        run_id, snapshot_hash, snapshot_root_hash,
                        preflight_run_id, target_hash, target_database,
                        batch_rows, status, started_at, started_by, completed_at,
                        write_credential_binding_hash, write_principal_hash,
                        write_permission_hash, write_context_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_run_id,
                        run.snapshot_hash,
                        run.snapshot_root_hash,
                        canonical_preflight_id,
                        run.target_hash,
                        run.target_database,
                        run.batch_rows,
                        run.status.value,
                        run.started_at.isoformat(),
                        run.started_by,
                        None,
                        run.write_credential_binding_hash,
                        run.write_principal_hash,
                        run.write_permission_hash,
                        run.write_context_hash,
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO execution_row VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        [
                            canonical_run_id,
                            ordinal,
                            item.row_id,
                            item.dataset,
                            item.target_model,
                            item.operation,
                            item.status.value,
                            item.attempt,
                            item.odoo_id,
                            item.safe_error,
                            item.to_json(),
                            run.started_at.isoformat(),
                        ]
                        for ordinal, item in enumerate(run.rows)
                    ],
                )
                connection.execute(
                    "INSERT OR REPLACE INTO execution_current VALUES (1, ?)",
                    [canonical_run_id],
                )
                revision = self._workspace_revision(connection)
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type=(
                        "ODOO_CORRECTION_STARTED"
                        if correction
                        else (
                            "ODOO_TRANSFER_LOAD_STARTED"
                            if transfer
                            else "ODOO_LOAD_STARTED"
                        )
                    ),
                    detail=(
                        f"run {canonical_run_id}: {len(run.rows)} planned row(s), "
                        f"{run.batch_rows} row(s) per Odoo batch; "
                        "write principal "
                        f"{run.write_principal_hash or 'unverified-local'}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Commit one completed Odoo call's row outcomes in a short transaction."""

        if not rows:
            return
        self._assert_workspace_mutable(workspace_id)
        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Execution run identifier is invalid") from error
        if any(
            item.status
            not in {
                ExecutionRowStatus.COMMITTED,
                ExecutionRowStatus.PARTIALLY_APPLIED,
                ExecutionRowStatus.FAILED,
                ExecutionRowStatus.BLOCKED,
                ExecutionRowStatus.OUTCOME_UNKNOWN,
            }
            or item.attempt < 0
            for item in rows
        ):
            raise WorkspaceError("Execution outcome is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                run_status = connection.execute(
                    "SELECT status FROM execution_run WHERE run_id = ?",
                    [canonical_run_id],
                ).fetchone()
                if run_status is None or str(run_status[0]) != "RUNNING":
                    raise WorkspaceError("Execution run is not active")
                for item in rows:
                    stored = connection.execute(
                        """
                        SELECT row_json FROM execution_row
                         WHERE run_id = ? AND row_id = ?
                        """,
                        [canonical_run_id, item.row_id],
                    ).fetchone()
                    if stored is None:
                        raise WorkspaceError("Execution outcome row is missing")
                    previous = ExecutionRowAttempt.from_json(str(stored[0]))
                    if (
                        previous.status
                        in {
                            ExecutionRowStatus.PLANNED,
                            ExecutionRowStatus.RETRY_READY,
                        }
                        and item.status is ExecutionRowStatus.BLOCKED
                    ):
                        pass
                    elif previous.status is ExecutionRowStatus.IN_FLIGHT:
                        pass
                    elif previous.status is ExecutionRowStatus.PARTIALLY_APPLIED and item.status in {
                        ExecutionRowStatus.PARTIALLY_APPLIED,
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.OUTCOME_UNKNOWN,
                    }:
                        pass
                    else:
                        raise WorkspaceError("Execution row was already attempted")
                    stable_before = (
                        previous.row_id,
                        previous.dataset,
                        previous.source_row,
                        previous.target_model,
                        previous.operation,
                        previous.field_names,
                        previous.proposed_external_id,
                        previous.attempt,
                        previous.schedule_component,
                        previous.transport_page,
                        previous.transport_batch,
                        previous.transport_phase,
                        previous.recovery_hash,
                    )
                    stable_after = (
                        item.row_id,
                        item.dataset,
                        item.source_row,
                        item.target_model,
                        item.operation,
                        item.field_names,
                        item.proposed_external_id,
                        item.attempt,
                        item.schedule_component,
                        item.transport_page,
                        item.transport_batch,
                        item.transport_phase,
                        item.recovery_hash,
                    )
                    if stable_before != stable_after:
                        raise WorkspaceError("Execution outcome transition is invalid")
                    previous_projected = {
                        (receipt.projection_field, receipt.target_model): receipt.odoo_id
                        for receipt in previous.projected_receipts
                    }
                    current_projected = {
                        (receipt.projection_field, receipt.target_model): receipt.odoo_id
                        for receipt in item.projected_receipts
                    }
                    if (
                        any(
                            current_projected.get(key) != identifier
                            for key, identifier in previous_projected.items()
                        )
                        or (
                            current_projected != previous_projected
                            and previous.status
                            is not ExecutionRowStatus.PARTIALLY_APPLIED
                        )
                    ):
                        raise WorkspaceError(
                            "Execution projected Odoo receipt changed"
                        )
                    if (
                        previous.status is ExecutionRowStatus.IN_FLIGHT
                        and previous.transport_phase in {"UPDATE", "COMPLETION"}
                        and item.odoo_id != previous.odoo_id
                    ) or (
                        previous.status is ExecutionRowStatus.PARTIALLY_APPLIED
                        and item.odoo_id != previous.odoo_id
                    ):
                        raise WorkspaceError("Execution Odoo receipt changed")
                    connection.execute(
                        """
                        UPDATE execution_row
                           SET status = ?, attempt = ?, odoo_id = ?,
                               safe_error = ?, row_json = ?, updated_at = ?
                         WHERE run_id = ? AND row_id = ?
                        """,
                        [
                            item.status.value,
                            item.attempt,
                            item.odoo_id,
                            item.safe_error[:500],
                            item.to_json(),
                            datetime.now(timezone.utc).isoformat(),
                            canonical_run_id,
                            item.row_id,
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_batch_started(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Persist one exact in-flight batch before Odoo transport."""

        if not rows:
            raise WorkspaceError("Execution transport batch is empty")
        self._assert_workspace_mutable(workspace_id)
        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Execution run identifier is invalid") from error
        phases = {item.transport_phase for item in rows}
        components = {item.schedule_component for item in rows}
        pages = {item.transport_page for item in rows}
        batches = {item.transport_batch for item in rows}
        models = {item.target_model for item in rows}
        if (
            any(item.status is not ExecutionRowStatus.IN_FLIGHT for item in rows)
            or phases not in ({"CREATE"}, {"UPDATE"}, {"COMPLETION"})
            or len(components) != 1
            or min(components) < 0
            or len(pages) != 1
            or min(pages) < 0
            or len(batches) != 1
            or min(batches) < 0
            or len(models) != 1
            or len({item.row_id for item in rows}) != len(rows)
        ):
            raise WorkspaceError("Execution transport batch is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                run_status = connection.execute(
                    "SELECT status FROM execution_run WHERE run_id = ?",
                    [canonical_run_id],
                ).fetchone()
                if run_status is None or str(run_status[0]) != "RUNNING":
                    raise WorkspaceError("Execution run is not active")
                for item in rows:
                    stored = connection.execute(
                        "SELECT row_json FROM execution_row WHERE run_id = ? AND row_id = ?",
                        [canonical_run_id, item.row_id],
                    ).fetchone()
                    if stored is None:
                        raise WorkspaceError("Execution transport row is missing")
                    previous = ExecutionRowAttempt.from_json(str(stored[0]))
                    expected_status = (
                        ExecutionRowStatus.PARTIALLY_APPLIED
                        if item.transport_phase == "COMPLETION"
                        else (
                            ExecutionRowStatus.RETRY_READY
                            if previous.status is ExecutionRowStatus.RETRY_READY
                            else ExecutionRowStatus.PLANNED
                        )
                    )
                    if (
                        previous.status is not expected_status
                        or item.attempt != previous.attempt + 1
                        or item.schedule_component != previous.schedule_component
                        or item.row_id != previous.row_id
                        or item.dataset != previous.dataset
                        or item.source_row != previous.source_row
                        or item.target_model != previous.target_model
                        or item.operation != previous.operation
                        or item.field_names != previous.field_names
                        or item.proposed_external_id != previous.proposed_external_id
                        or item.recovery_hash != previous.recovery_hash
                        or item.projected_receipts != previous.projected_receipts
                        or item.safe_error
                        or (
                            item.transport_phase == "CREATE"
                            and (
                                item.operation != "CREATE"
                                or item.odoo_id is not None
                            )
                        )
                        or (
                            item.transport_phase == "UPDATE"
                            and (
                                item.operation != "UPDATE"
                                or item.odoo_id is None
                            )
                        )
                        or (
                            item.transport_phase == "COMPLETION"
                            and (
                                item.operation != "CREATE"
                                or item.odoo_id is None
                            )
                        )
                    ):
                        raise WorkspaceError("Execution transport transition is invalid")
                    connection.execute(
                        """
                        UPDATE execution_row
                           SET status = ?, attempt = ?, odoo_id = ?,
                               safe_error = ?, row_json = ?, updated_at = ?
                         WHERE run_id = ? AND row_id = ?
                        """,
                        [
                            item.status.value,
                            item.attempt,
                            item.odoo_id,
                            item.safe_error[:500],
                            item.to_json(),
                            datetime.now(timezone.utc).isoformat(),
                            canonical_run_id,
                            item.row_id,
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_recovery(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        *,
        actor: Actor,
    ) -> None:
        """Atomically bind every resumable row to one read-back report."""

        self._assert_workspace_mutable(workspace_id)
        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Execution run identifier is invalid") from error
        recovery_hashes = {item.recovery_hash for item in rows}
        if (
            not rows
            or len({item.row_id for item in rows}) != len(rows)
            or len(recovery_hashes) != 1
            or not _SHA256.fullmatch(next(iter(recovery_hashes), ""))
        ):
            raise WorkspaceError("Execution recovery evidence is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                run_status = connection.execute(
                    "SELECT status FROM execution_run WHERE run_id = ?",
                    [canonical_run_id],
                ).fetchone()
                stored_values = connection.execute(
                    """
                    SELECT row_id, row_json FROM execution_row
                     WHERE run_id = ? ORDER BY ordinal
                    """,
                    [canonical_run_id],
                ).fetchall()
                if run_status is None or str(run_status[0]) != "RUNNING":
                    raise WorkspaceError("Execution run is not active")
                stored = {
                    str(row_id): ExecutionRowAttempt.from_json(str(row_json))
                    for row_id, row_json in stored_values
                }
                if set(stored) != {item.row_id for item in rows}:
                    raise WorkspaceError(
                        "Execution recovery does not cover every journal row"
                    )
                allowed = {
                    ExecutionRowStatus.PLANNED: {
                        ExecutionRowStatus.PLANNED,
                    },
                    ExecutionRowStatus.RETRY_READY: {
                        ExecutionRowStatus.RETRY_READY,
                    },
                    ExecutionRowStatus.IN_FLIGHT: {
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.PARTIALLY_APPLIED,
                        ExecutionRowStatus.RETRY_READY,
                    },
                    ExecutionRowStatus.PARTIALLY_APPLIED: {
                        ExecutionRowStatus.PARTIALLY_APPLIED,
                        ExecutionRowStatus.COMMITTED,
                    },
                    ExecutionRowStatus.COMMITTED: {
                        ExecutionRowStatus.COMMITTED,
                    },
                }
                for item in rows:
                    previous = stored[item.row_id]
                    stable_before = (
                        previous.row_id,
                        previous.dataset,
                        previous.source_row,
                        previous.target_model,
                        previous.operation,
                        previous.field_names,
                        previous.proposed_external_id,
                        previous.attempt,
                        previous.schedule_component,
                        previous.transport_page,
                        previous.transport_batch,
                        previous.transport_phase,
                    )
                    stable_after = (
                        item.row_id,
                        item.dataset,
                        item.source_row,
                        item.target_model,
                        item.operation,
                        item.field_names,
                        item.proposed_external_id,
                        item.attempt,
                        item.schedule_component,
                        item.transport_page,
                        item.transport_batch,
                        item.transport_phase,
                    )
                    if (
                        stable_before != stable_after
                        or item.status not in allowed.get(previous.status, set())
                        or item.projected_receipts != previous.projected_receipts
                    ):
                        raise WorkspaceError(
                            "Execution recovery transition is invalid"
                        )
                    connection.execute(
                        """
                        UPDATE execution_row
                           SET status = ?, attempt = ?, odoo_id = ?,
                               safe_error = ?, row_json = ?, updated_at = ?
                         WHERE run_id = ? AND row_id = ?
                        """,
                        [
                            item.status.value,
                            item.attempt,
                            item.odoo_id,
                            item.safe_error[:500],
                            item.to_json(),
                            datetime.now(timezone.utc).isoformat(),
                            canonical_run_id,
                            item.row_id,
                        ],
                    )
                revision = self._workspace_revision(connection)
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="ODOO_LOAD_RECOVERY_CONFIRMED",
                    detail=(
                        f"run {canonical_run_id}: recovery "
                        f"{next(iter(recovery_hashes))}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def finish_run(
        self,
        workspace_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun:
        self._assert_workspace_mutable(workspace_id)
        if status is ExecutionRunStatus.RUNNING:
            raise WorkspaceError("Execution completion status is invalid")
        canonical_run_id = str(UUID(run_id))
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT status FROM execution_run WHERE run_id = ?",
                    [canonical_run_id],
                ).fetchone()
                if current is None or str(current[0]) != "RUNNING":
                    raise WorkspaceError("Execution run is not active")
                planned = connection.execute(
                    """
                    SELECT COUNT(*) FROM execution_row
                     WHERE run_id = ?
                       AND status IN ('PLANNED', 'IN_FLIGHT', 'RETRY_READY')
                    """,
                    [canonical_run_id],
                ).fetchone()
                if planned is not None and int(planned[0]) != 0:
                    raise WorkspaceError("Execution run still has unattempted rows")
                completed_at = datetime.now(timezone.utc)
                connection.execute(
                    """
                    UPDATE execution_run SET status = ?, completed_at = ?
                     WHERE run_id = ?
                    """,
                    [status.value, completed_at.isoformat(), canonical_run_id],
                )
                revision = self._workspace_revision(connection)
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="ODOO_LOAD_FINISHED",
                    detail=f"run {canonical_run_id}: {status.value}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        result = self.get_run(workspace_id, canonical_run_id)
        if result is None:
            raise WorkspaceError("Execution run could not be reloaded")
        return result

    def get_current_run(
        self,
        workspace_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None:
        query = """
            SELECT run.run_id
              FROM execution_current AS current
              JOIN execution_run AS run ON run.run_id = current.run_id
             WHERE current.singleton_id = 1
        """
        parameters: list[object] = []
        if snapshot_hash is not None:
            query += " AND run.snapshot_hash = ?"
            parameters.append(snapshot_hash)
        values = self._read_json_rows(workspace_id, query, parameters)
        return self.get_run(workspace_id, values[0]) if values else None

    def get_run(self, workspace_id: str, run_id: str) -> ExecutionRun | None:
        canonical_run_id = str(UUID(run_id))
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            header = connection.execute(
                """
                SELECT snapshot_hash, snapshot_root_hash, preflight_run_id,
                       target_hash, target_database, batch_rows, status,
                       started_at, started_by, completed_at,
                       write_credential_binding_hash, write_principal_hash,
                       write_permission_hash, write_context_hash
                  FROM execution_run WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            row_values = connection.execute(
                """
                SELECT row_json FROM execution_row
                 WHERE run_id = ? ORDER BY ordinal
                """,
                [canonical_run_id],
            ).fetchall()
        if header is None:
            return None
        return ExecutionRun(
            run_id=canonical_run_id,
            workspace_id=workspace_id,
            snapshot_hash=str(header[0]),
            snapshot_root_hash=str(header[1]),
            preflight_run_id=str(header[2]),
            target_hash=str(header[3]),
            target_database=str(header[4]),
            batch_rows=(int(header[5]) if header[5] is not None else None),
            status=ExecutionRunStatus(str(header[6])),
            started_at=datetime.fromisoformat(str(header[7])),
            started_by=str(header[8]),
            completed_at=(
                datetime.fromisoformat(str(header[9]))
                if header[9] is not None
                else None
            ),
            rows=tuple(
                ExecutionRowAttempt.from_json(str(item[0])) for item in row_values
            ),
            write_credential_binding_hash=str(header[10] or ""),
            write_principal_hash=str(header[11] or ""),
            write_permission_hash=str(header[12] or ""),
            write_context_hash=str(header[13] or ""),
        )
