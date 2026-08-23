"""Persist hash-bound post-write Odoo read-back results."""

from __future__ import annotations

from uuid import UUID

from ...access import Actor
from ...domain.reconciliation import ReconciliationRun
from ...workspace_state import WorkspaceStateNotFoundError
from ...workspace_errors import WorkspaceError
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository


class ReconciliationRepository(DuckDbRepository):
    """Own one immutable result for the current practical load."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        super().__init__(database)

    def publish(
        self,
        project_id: str,
        report: ReconciliationRun,
        *,
        actor: Actor,
    ) -> None:
        try:
            report = ReconciliationRun.from_json(report.to_json())
        except (KeyError, TypeError, ValueError) as error:
            raise WorkspaceError("Verification result is invalid") from error
        try:
            reconciliation_id = str(UUID(report.reconciliation_id))
            execution_run_id = str(UUID(report.execution_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Verification result identifier is invalid") from error
        if report.project_id != project_id:
            raise WorkspaceError("Verification result belongs to another project")
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
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
                previous = connection.execute(
                    """
                    SELECT report_hash FROM reconciliation_run
                     WHERE execution_run_id = ?
                    """,
                    [execution_run_id],
                ).fetchone()
                if previous is not None:
                    if str(previous[0]) != report.semantic_hash:
                        raise WorkspaceError("This load already has another result")
                    connection.rollback()
                    return
                connection.execute(
                    """
                    INSERT INTO reconciliation_run
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    event_type="ODOO_LOAD_VERIFIED",
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

    def get_current(
        self,
        project_id: str,
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
        rows = self._read_json_rows(project_id, query, parameters)
        return self.get(project_id, rows[0]) if rows else None

    def get(self, project_id: str, reconciliation_id: str) -> ReconciliationRun | None:
        canonical_id = str(UUID(reconciliation_id))
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
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
        if report.project_id != project_id:
            raise WorkspaceError("Verification result belongs to another project")
        return report

