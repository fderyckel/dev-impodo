"""Persist practical load runs without spanning Odoo network calls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from ...access import Actor
from ...domain.execution import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from ...projects import ProjectNotFoundError
from ...workspace_errors import WorkspaceError
from .database import DuckDbDatabase
from .repository import DuckDbRepository


class ExecutionRepository(DuckDbRepository):
    """Own the target-specific row journal and current execution pointer."""

    def __init__(self, database: DuckDbDatabase) -> None:
        super().__init__(database)

    def start_run(
        self,
        project_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
    ) -> None:
        try:
            canonical_run_id = str(UUID(run.run_id))
            canonical_preflight_id = str(UUID(run.preflight_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Execution run identifier is invalid") from error
        if (
            run.project_id != project_id
            or run.status is not ExecutionRunStatus.RUNNING
            or run.completed_at is not None
            or not run.rows
            or any(item.status is not ExecutionRowStatus.PLANNED for item in run.rows)
        ):
            raise WorkspaceError("Execution run is invalid")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
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
                        "The load preview is no longer current. Compare with Odoo again."
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
                    INSERT INTO execution_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_run_id,
                        run.snapshot_hash,
                        run.snapshot_root_hash,
                        canonical_preflight_id,
                        run.target_hash,
                        run.target_database,
                        run.status.value,
                        run.started_at.isoformat(),
                        run.started_by,
                        None,
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
                revision = self._project_revision(connection)
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="ODOO_LOAD_STARTED",
                    detail=(
                        f"run {canonical_run_id}: {len(run.rows)} planned row(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_outcomes(
        self,
        project_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Commit one completed Odoo call's row outcomes in a short transaction."""

        if not rows:
            return
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
            or (
                item.attempt != 1
                if item.status is not ExecutionRowStatus.BLOCKED
                else item.attempt != 0
            )
            for item in rows
        ):
            raise WorkspaceError("Execution outcome is invalid")
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                for item in rows:
                    stored = connection.execute(
                        """
                        SELECT status FROM execution_row
                         WHERE run_id = ? AND row_id = ?
                        """,
                        [canonical_run_id, item.row_id],
                    ).fetchone()
                    stored_status = str(stored[0]) if stored is not None else ""
                    if stored_status == "PLANNED":
                        pass
                    elif stored_status == "PARTIALLY_APPLIED" and item.status in {
                        ExecutionRowStatus.PARTIALLY_APPLIED,
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.OUTCOME_UNKNOWN,
                    }:
                        pass
                    else:
                        raise WorkspaceError("Execution row was already attempted")
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

    def finish_run(
        self,
        project_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun:
        if status is ExecutionRunStatus.RUNNING:
            raise WorkspaceError("Execution completion status is invalid")
        canonical_run_id = str(UUID(run_id))
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
                     WHERE run_id = ? AND status = 'PLANNED'
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
                revision = self._project_revision(connection)
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
        result = self.get_run(project_id, canonical_run_id)
        if result is None:
            raise WorkspaceError("Execution run could not be reloaded")
        return result

    def get_current_run(
        self,
        project_id: str,
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
        values = self._read_json_rows(project_id, query, parameters)
        return self.get_run(project_id, values[0]) if values else None

    def get_run(self, project_id: str, run_id: str) -> ExecutionRun | None:
        canonical_run_id = str(UUID(run_id))
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            header = connection.execute(
                """
                SELECT snapshot_hash, snapshot_root_hash, preflight_run_id,
                       target_hash, target_database, status, started_at,
                       started_by, completed_at
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
            project_id=project_id,
            snapshot_hash=str(header[0]),
            snapshot_root_hash=str(header[1]),
            preflight_run_id=str(header[2]),
            target_hash=str(header[3]),
            target_database=str(header[4]),
            status=ExecutionRunStatus(str(header[5])),
            started_at=datetime.fromisoformat(str(header[6])),
            started_by=str(header[7]),
            completed_at=(
                datetime.fromisoformat(str(header[8]))
                if header[8] is not None
                else None
            ),
            rows=tuple(
                ExecutionRowAttempt.from_json(str(item[0])) for item in row_values
            ),
        )
