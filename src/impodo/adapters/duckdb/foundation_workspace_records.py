"""Persist MigrationWorkspace registry records and access lineage."""

from __future__ import annotations

from impodo.domain.shared.access import Actor
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationNotFoundError,
    require_revision,
    require_uuid,
)
from ...domain.workspace.models import MigrationWorkspace
from impodo.application.workspace.access import WorkspaceAccessContext


class FoundationWorkspaceRecords:
    """Own workspace registry reads, access verification, and revisions."""

    def get_migration_workspace(self, workspace_id: str) -> MigrationWorkspace:
        workspace = self._get_workspace_registry(workspace_id)
        self.database.ensure_workspace_store(workspace)
        return workspace

    def _get_workspace_registry(self, workspace_id: str) -> MigrationWorkspace:
        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            row = self._exact_row(
                connection,
                table="migration_workspace",
                id_column="workspace_id",
                identity=workspace_id,
                expected_kind="MIGRATION_WORKSPACE",
            )
        return self._workspace_from_row(row)

    def resolve_workspace_access_context(
        self,
        workspace_id: str,
    ) -> WorkspaceAccessContext:
        """Resolve and verify all workspace lineage in one registry query."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT w.project_id, w.workspace_id, w.data_version_id,
                       w.migration_run_id, w.recipe_application_id, r.purpose
                  FROM migration_workspace w
                  JOIN migration_project p
                    ON p.project_id = w.project_id
                  JOIN data_version d
                    ON d.data_version_id = w.data_version_id
                   AND d.project_id = w.project_id
                  JOIN migration_run r
                    ON r.migration_run_id = w.migration_run_id
                   AND r.project_id = w.project_id
                   AND r.data_version_id = w.data_version_id
             LEFT JOIN recipe_application a
                    ON a.application_id = w.recipe_application_id
                   AND a.project_id = w.project_id
                   AND a.migration_run_id = w.migration_run_id
                   AND a.data_version_id = w.data_version_id
                   AND a.workspace_id = w.workspace_id
                 WHERE w.workspace_id = ?
                   AND (
                       w.recipe_application_id IS NULL
                       OR a.application_id IS NOT NULL
                   )
                """,
                [workspace_id],
            ).fetchone()
        if row is None:
            raise MigrationNotFoundError(
                "Verified MigrationWorkspace access context not found"
            )
        return WorkspaceAccessContext(
            project_id=str(row[0]),
            workspace_id=str(row[1]),
            data_version_id=str(row[2]),
            migration_run_id=str(row[3]),
            recipe_application_id=str(row[4]) if row[4] else None,
            run_purpose=str(row[5]),
        )

    def list_migration_workspaces(
        self,
        migration_run_id: str,
    ) -> tuple[MigrationWorkspace, ...]:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            if connection.execute(
                "SELECT 1 FROM migration_run WHERE migration_run_id = ?",
                [migration_run_id],
            ).fetchone() is None:
                self._raise_missing_identity(connection, migration_run_id)
            rows = self._rows(
                connection,
                "SELECT * FROM migration_workspace WHERE migration_run_id = ? "
                "ORDER BY created_at, workspace_id",
                [migration_run_id],
            )
        return tuple(self._workspace_from_row(row) for row in rows)

    def list_project_migration_workspaces(
        self,
        project_id: str,
    ) -> tuple[MigrationWorkspace, ...]:
        """Return one bounded Project workspace projection without N+1 reads."""

        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            self._require_project(connection, project_id)
            rows = self._rows(
                connection,
                "SELECT * FROM migration_workspace WHERE project_id = ? "
                "ORDER BY created_at, workspace_id",
                [project_id],
            )
        return tuple(self._workspace_from_row(row) for row in rows)

    def save_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationWorkspace:
        expected_revision = require_revision(expected_revision)
        if workspace.optimistic_revision != expected_revision:
            raise MigrationConflictError("MigrationWorkspace revision is stale")
        new_revision = expected_revision + 1
        with self._registry_transactions.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE migration_workspace
                       SET display_name = ?, state = ?, setup_state = ?,
                       optimistic_revision = ?, updated_at = ?,
                       setup_completed_at = ?, closed_at = ?
                 WHERE workspace_id = ? AND optimistic_revision = ?
                 RETURNING workspace_id
                """,
                [
                    workspace.display_name,
                    workspace.state.value,
                    workspace.setup_state.value,
                    new_revision,
                    workspace.updated_at.isoformat(),
                    self._time(workspace.setup_completed_at),
                    self._time(workspace.closed_at),
                    workspace.workspace_id,
                    expected_revision,
                ],
            ).fetchone()
            if updated is None:
                raise MigrationConflictError(
                    "MigrationWorkspace changed; reload and retry"
                )
            self._insert_event(
                connection,
                project_id=workspace.project_id,
                aggregate_kind="MIGRATION_WORKSPACE",
                aggregate_id=workspace.workspace_id,
                aggregate_revision=new_revision,
                event_type=event_type,
                detail={},
                actor=actor,
                occurred_at=workspace.updated_at,
            )
        return self.get_migration_workspace(workspace.workspace_id)

    def project_id_for_workspace(self, workspace_id: str) -> str:
        return self._get_workspace_registry(workspace_id).project_id
