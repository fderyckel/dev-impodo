"""Read and transition value-free preparation-session lifecycle state."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from ...domain.staging.preparation_session import (
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparationSessionSummary,
)
from ...staging import StagingRunStatus
from ...workspace_errors import WorkspaceError


_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class PreparationSessionLifecycle:
    """Own session status summaries and terminal cleanup transitions."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def get(self, workspace_id: str, session_id: str) -> PreparationSessionSummary:
        """Return one value-free session status projection."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT status, mapping_id, mapping_version,
                       physical_selection_hash, source_selection_hash,
                       mapping_hash, schema_hash, derived_plan_hash,
                       compiled_plan_hash, contract_version,
                       evaluator_version, source_hashes_json,
                       staged_row_count, canonical_row_count,
                       impact_row_count, failure_code
                  FROM preparation_session
                 WHERE session_id = ?
                """,
                [self._repository._session_id(session_id)],
            ).fetchone()
        if row is None:
            raise WorkspaceError("Preparation session was not found")
        try:
            bindings = PreparationSessionBindings(
                mapping_id=str(row[1]),
                mapping_version=int(row[2]),
                physical_selection_hash=str(row[3]),
                source_selection_hash=str(row[4]),
                mapping_hash=str(row[5]),
                schema_hash=str(row[6]),
                derived_plan_hash=str(row[7]) if row[7] else None,
                compiled_plan_hash=str(row[8]),
                contract_version=int(row[9]),
                evaluator_version=int(row[10]),
                source_hashes={
                    str(key): str(value)
                    for key, value in json.loads(str(row[11])).items()
                },
            )
            return PreparationSessionSummary(
                session_id=session_id,
                status=PreparationSessionStatus(str(row[0])),
                bindings=bindings,
                staged_row_count=int(row[12]),
                canonical_row_count=int(row[13]),
                impact_row_count=int(row[14]),
                failure_code=str(row[15]) if row[15] else None,
            )
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Preparation session header is invalid") from error

    def mark_published(self, workspace_id: str, session_id: str) -> None:
        """Retain value-free status metadata and remove temporary evidence."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._repository._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.READY,
                )
                bound_snapshots = connection.execute(
                    """
                    SELECT binding.dataset_id, binding.content_hash
                      FROM preparation_session_snapshot AS binding
                      JOIN prepared_snapshot_manifest AS manifest
                        ON manifest.content_hash = binding.content_hash
                       AND manifest.dataset_id = binding.dataset_id
                     WHERE binding.session_id = ?
                     ORDER BY binding.dataset_id
                    """,
                    [self._repository._session_id(session_id)],
                ).fetchall()
                for dataset_id, content_hash in bound_snapshots:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO prepared_snapshot_current
                        VALUES (?, ?)
                        """,
                        [str(dataset_id), str(content_hash)],
                    )
                bound_derived_artifacts = connection.execute(
                    """
                    SELECT binding.dataset_id, binding.content_hash,
                           manifest.manifest_json
                      FROM preparation_session_derived_artifact AS binding
                      LEFT JOIN derived_value_artifact_manifest AS manifest
                        ON manifest.content_hash = binding.content_hash
                       AND manifest.dataset_id = binding.dataset_id
                     WHERE binding.session_id = ?
                     ORDER BY binding.dataset_id
                    """,
                    [self._repository._session_id(session_id)],
                ).fetchall()
                self._repository._derived_artifacts_from_bindings(
                    bound_derived_artifacts
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO derived_value_artifact_current
                    SELECT binding.dataset_id, binding.content_hash
                      FROM preparation_session_derived_artifact AS binding
                      JOIN derived_value_artifact_manifest AS manifest
                        ON manifest.content_hash = binding.content_hash
                       AND manifest.dataset_id = binding.dataset_id
                     WHERE binding.session_id = ?
                    """,
                    [self._repository._session_id(session_id)],
                )
                canonical_status = connection.execute(
                    """
                    SELECT status
                      FROM canonical_staging_run
                     WHERE run_id = ?
                    """,
                    [self._repository._session_id(session_id)],
                ).fetchone()
                self._repository._delete_session_rows(
                    connection,
                    session_id,
                    retain_relationships=(
                        canonical_status is not None
                        and str(canonical_status[0])
                        != StagingRunStatus.PENDING.value
                    ),
                )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.PUBLISHED.value,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def fail(
        self,
        workspace_id: str,
        session_id: str,
        failure_code: str,
    ) -> None:
        """Fail closed with a non-sensitive code and remove temporary values."""

        if _FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("Preparation failure code is invalid")
        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            return
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                exists = connection.execute(
                    "SELECT 1 FROM preparation_session WHERE session_id = ?",
                    [self._repository._session_id(session_id)],
                ).fetchone()
                if exists is None:
                    connection.rollback()
                    return
                self._repository._delete_session_rows(connection, session_id)
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, failure_code = ?, updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.FAILED.value,
                        failure_code,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
