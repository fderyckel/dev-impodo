"""Delete all unpublished rows owned by one failed preparation session."""

from __future__ import annotations

from uuid import UUID

from impodo.domain.preparation.staging import StagingRunStatus
from impodo.domain.workspace.errors import WorkspaceError


class PreparationFailureCleanup:
    @staticmethod
    def _delete_session_rows(
        connection,
        session_id: str,
        *,
        retain_relationships: bool = False,
    ) -> None:
        canonical = PreparationFailureCleanup._session_id(session_id)
        connection.execute(
            """
            DELETE FROM normalization_effect
             WHERE run_id = ?
               AND NOT EXISTS (
                   SELECT 1 FROM normalization_run WHERE run_id = ?
               )
            """,
            [canonical, canonical],
        )
        session_tables = (
            "preparation_normalization_finding",
            "preparation_normalization_group_seed",
            "preparation_impact_row",
            "preparation_physical_row",
            "preparation_lineage",
            "preparation_identity_group",
            "preparation_relationship_edge",
            "preparation_direct_identity",
            "preparation_session_snapshot",
            "preparation_session_derived_artifact",
        )
        available_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = current_schema()
                   AND table_name = ANY(?)
                """,
                [list(session_tables)],
            ).fetchall()
        }
        for table in session_tables:
            if table not in available_tables:
                continue
            if retain_relationships and table == "preparation_relationship_edge":
                continue
            connection.execute(
                f"DELETE FROM {table} WHERE session_id = ?",
                [canonical],
            )
        pending = connection.execute(
            """
            SELECT 1
              FROM canonical_staging_run
             WHERE run_id = ? AND status = ?
            """,
            [canonical, StagingRunStatus.PENDING.value],
        ).fetchone()
        if pending is not None:
            connection.execute(
                "DELETE FROM canonical_staging_row_issue WHERE run_id = ?",
                [canonical],
            )
            connection.execute(
                "DELETE FROM canonical_prepared_projection WHERE run_id = ?",
                [canonical],
            )
            connection.execute(
                "DELETE FROM canonical_staging_row WHERE run_id = ?",
                [canonical],
            )
            connection.execute(
                "DELETE FROM canonical_staging_run WHERE run_id = ?",
                [canonical],
            )

    @staticmethod
    def _session_id(session_id: str) -> str:
        try:
            return str(UUID(session_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Preparation session identifier is invalid") from error
