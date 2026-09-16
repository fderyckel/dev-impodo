"""Persist hash-bound post-write Odoo read-back results."""

from __future__ import annotations

from uuid import UUID

from impodo.domain.shared.access import Actor
from ...domain.reconciliation import ReconciliationRun
from ...domain.reconciliation_detail import ReconciliationDetailManifest
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError
from impodo.domain.workspace.errors import WorkspaceError
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository


class ReconciliationRepository(DuckDbRepository):
    """Own append-only results and a current pointer for a practical load."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        super().__init__(database)

    def publish(
        self,
        workspace_id: str,
        report: ReconciliationRun,
        *,
        actor: Actor,
        detail: ReconciliationDetailManifest | None = None,
    ) -> None:
        self._assert_workspace_mutable(workspace_id)
        try:
            report = ReconciliationRun.from_json(report.to_json())
        except (KeyError, TypeError, ValueError) as error:
            raise WorkspaceError("Verification result is invalid") from error
        if report.readback_scope != "FULL":
            raise WorkspaceError("A targeted recovery assessment cannot be published as verification")
        try:
            reconciliation_id = str(UUID(report.reconciliation_id))
            execution_run_id = str(UUID(report.execution_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Verification result identifier is invalid") from error
        if report.workspace_id != workspace_id:
            raise WorkspaceError("Verification result belongs to another workspace")
        if detail is not None and detail.reconciliation_id != reconciliation_id:
            raise WorkspaceError("Verification detail belongs to another result")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT run.run_id, run.snapshot_hash, run.target_hash,
                           run.target_database, run.status
                      FROM execution_current AS current
                      JOIN execution_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                if current is None or (
                    str(current[0]) != execution_run_id
                    or str(current[1]) != report.snapshot_hash
                    or str(current[2]) != report.target_hash
                    or str(current[3]) != report.target_database
                    or str(current[4]) == "RUNNING"
                ):
                    raise WorkspaceError(
                        "The load outcome changed before verification was saved"
                    )
                recorded_row_ids = {
                    str(item[0])
                    for item in connection.execute(
                        "SELECT row_id FROM execution_row WHERE run_id = ?",
                        [execution_run_id],
                    ).fetchall()
                }
                if recorded_row_ids != {item.row_id for item in report.rows}:
                    raise WorkspaceError(
                        "Verification does not cover every written load row"
                    )
                previous = connection.execute(
                    """
                    SELECT report_hash FROM reconciliation_run
                     WHERE reconciliation_id = ?
                    """,
                    [reconciliation_id],
                ).fetchone()
                if previous is not None:
                    if str(previous[0]) != report.semantic_hash:
                        raise WorkspaceError("This verification identifier was reused")
                    connection.rollback()
                    return
                earlier_attempt = connection.execute(
                    """
                    SELECT COUNT(*) FROM reconciliation_run
                     WHERE execution_run_id = ?
                    """,
                    [execution_run_id],
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO reconciliation_run (
                        reconciliation_id, execution_run_id, snapshot_hash,
                        target_hash, target_database, status, verified_at,
                        verified_by, report_hash, report_json,
                        detail_storage_name, detail_logical_hash,
                        detail_artifact_hash, detail_size_bytes,
                        detail_difference_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        reconciliation_id,
                        execution_run_id,
                        report.snapshot_hash,
                        report.target_hash,
                        report.target_database,
                        report.status.value,
                        report.verified_at.isoformat(),
                        report.verified_by,
                        report.semantic_hash,
                        report.to_json(),
                        detail.storage_name if detail is not None else None,
                        detail.logical_hash if detail is not None else None,
                        detail.artifact_hash if detail is not None else None,
                        detail.size_bytes if detail is not None else None,
                        detail.difference_count if detail is not None else None,
                    ],
                )
                connection.execute(
                    "INSERT OR REPLACE INTO reconciliation_current VALUES (1, ?)",
                    [reconciliation_id],
                )
                revision = self._workspace_revision(connection)
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type=(
                        "ODOO_LOAD_REVERIFIED"
                        if earlier_attempt and int(earlier_attempt[0])
                        else "ODOO_LOAD_VERIFIED"
                    ),
                    detail=(
                        f"run {execution_run_id}: {report.status.value}; "
                        f"{report.verified_count} verified, "
                        f"{report.fallout_count} fallout"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_detail_manifest(
        self,
        workspace_id: str,
        reconciliation_id: str,
    ) -> ReconciliationDetailManifest | None:
        canonical_id = str(UUID(reconciliation_id))
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT detail_storage_name, detail_logical_hash,
                       detail_artifact_hash, detail_size_bytes,
                       detail_difference_count
                  FROM reconciliation_run
                 WHERE reconciliation_id = ?
                """,
                [canonical_id],
            ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return ReconciliationDetailManifest(
                reconciliation_id=canonical_id,
                storage_name=str(row[0]),
                logical_hash=str(row[1]),
                artifact_hash=str(row[2]),
                size_bytes=int(row[3]),
                difference_count=int(row[4]),
            )
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored verification detail is invalid") from error

    def history(
        self,
        workspace_id: str,
        execution_run_id: str,
    ) -> tuple[ReconciliationRun, ...]:
        canonical_run_id = str(UUID(execution_run_id))
        values = self._read_json_rows(
            workspace_id,
            """
            SELECT report_json FROM reconciliation_run
             WHERE execution_run_id = ?
             ORDER BY verified_at, reconciliation_id
            """,
            [canonical_run_id],
        )
        return tuple(ReconciliationRun.from_json(value) for value in values)

    def get_current(
        self,
        workspace_id: str,
        execution_run_id: str | None = None,
    ) -> ReconciliationRun | None:
        query = """
            SELECT run.reconciliation_id
              FROM reconciliation_current AS current
              JOIN reconciliation_run AS run
                ON run.reconciliation_id = current.reconciliation_id
             WHERE current.singleton_id = 1
        """
        parameters: list[object] = []
        if execution_run_id is not None:
            query += " AND run.execution_run_id = ?"
            parameters.append(str(UUID(execution_run_id)))
        rows = self._read_json_rows(workspace_id, query, parameters)
        return self.get(workspace_id, rows[0]) if rows else None

    def get(self, workspace_id: str, reconciliation_id: str) -> ReconciliationRun | None:
        canonical_id = str(UUID(reconciliation_id))
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT report_json FROM reconciliation_run
                 WHERE reconciliation_id = ?
                """,
                [canonical_id],
            ).fetchone()
        if row is None:
            return None
        report = ReconciliationRun.from_json(str(row[0]))
        if report.workspace_id != workspace_id:
            raise WorkspaceError("Verification result belongs to another workspace")
        return report
