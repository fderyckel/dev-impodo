"""Resolve the current mapping engine through clean MigrationWorkspace identity."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ...migration_foundation import (
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
    require_uuid,
)
from ...workspace_access import (
    WorkspaceAccessContext,
    current_workspace_access_context,
)
from .database import DuckDbWorkspaceDatabase
from .migration_foundation_database import MigrationFoundationDatabase
from .schema.migration_workspace_store import ensure_workspace_linkage


class MigrationWorkspaceEngineDatabase(DuckDbWorkspaceDatabase):
    """Place one mapping-engine database inside each clean workspace directory."""

    def __init__(
        self,
        foundation: MigrationFoundationDatabase,
        *,
        lock_wait_timeout_seconds: float = 0.0,
    ) -> None:
        super().__init__(
            foundation.root,
            lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        )
        self.foundation = foundation

    def workspace_directory(self, workspace_id: str) -> Path:
        """Resolve and verify one MigrationWorkspace before engine evidence reads."""

        try:
            workspace_id = str(UUID(workspace_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise MigrationNotFoundError("Invalid workspace_id") from error
        context = current_workspace_access_context()
        if context is not None:
            if context.workspace_id != workspace_id:
                raise MigrationIdentifierConfusionError(
                    "The command is bound to another MigrationWorkspace"
                )
        else:
            with self.foundation.connect(self.foundation.registry_path) as connection:
                row = connection.execute(
                    """
                    SELECT project_id, data_version_id, migration_run_id,
                           recipe_application_id
                      FROM migration_workspace
                     WHERE workspace_id = ?
                    """,
                    [workspace_id],
                ).fetchone()
            if row is None:
                raise MigrationNotFoundError("MigrationWorkspace not found")
            context = WorkspaceAccessContext(
                project_id=str(row[0]),
                workspace_id=workspace_id,
                data_version_id=str(row[1]),
                migration_run_id=str(row[2]),
                recipe_application_id=(str(row[3]) if row[3] is not None else None),
            )
        directory = self.foundation.workspace_directory(
            context.project_id,
            context.workspace_id,
        )
        store_path = directory / "workspace.duckdb"
        if not store_path.is_file():
            raise MigrationNotFoundError("MigrationWorkspace linkage is missing")
        with self.foundation.connect(store_path) as connection:
            ensure_workspace_linkage(connection, store_path, context)
        return directory

    def workspace_access_context(self, workspace_id: str) -> WorkspaceAccessContext:
        """Return the exact lineage already checked by ``workspace_directory``."""

        self.workspace_directory(workspace_id)
        bound = current_workspace_access_context()
        if bound is not None:
            return bound
        with self.foundation.connect(self.foundation.registry_path) as connection:
            row = connection.execute(
                """
                SELECT project_id, data_version_id, migration_run_id,
                       recipe_application_id
                  FROM migration_workspace
                 WHERE workspace_id = ?
                """,
                [workspace_id],
            ).fetchone()
        if row is None:
            raise MigrationNotFoundError("MigrationWorkspace not found")
        return WorkspaceAccessContext(
            project_id=str(row[0]),
            workspace_id=workspace_id,
            data_version_id=str(row[1]),
            migration_run_id=str(row[2]),
            recipe_application_id=(str(row[3]) if row[3] is not None else None),
        )

    def resolve_workspace_access_context(
        self,
        workspace_id: str,
    ) -> WorkspaceAccessContext:
        return self.workspace_access_context(workspace_id)


class FixedMigrationWorkspaceEngineDatabase(DuckDbWorkspaceDatabase):
    """Resolve one authorized workspace without opening the shared registry."""

    def __init__(
        self,
        root: str | Path,
        *,
        project_id: str,
        workspace_id: str,
        data_version_id: str,
        migration_run_id: str,
        recipe_application_id: str | None,
        lock_wait_timeout_seconds: float = 0.0,
    ) -> None:
        super().__init__(
            root,
            lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        )
        self.business_project_id = require_uuid(project_id, "project_id")
        self.workspace_id = require_uuid(workspace_id, "workspace_id")
        self.data_version_id = require_uuid(data_version_id, "data_version_id")
        self.migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        self.recipe_application_id = (
            require_uuid(recipe_application_id, "recipe_application_id")
            if recipe_application_id is not None
            else None
        )

    def workspace_directory(self, workspace_id: str) -> Path:
        if require_uuid(workspace_id, "workspace_id") != self.workspace_id:
            raise MigrationIdentifierConfusionError(
                "The worker was asked to open another MigrationWorkspace"
            )
        directory = (
            self.root
            / "projects"
            / self.business_project_id
            / "workspaces"
            / self.workspace_id
        )
        store_path = directory / "workspace.duckdb"
        if not store_path.is_file():
            raise MigrationNotFoundError("MigrationWorkspace linkage is missing")
        context = self.workspace_access_context(workspace_id)
        with self._connect(store_path) as connection:
            ensure_workspace_linkage(connection, store_path, context)
        return directory

    def workspace_access_context(self, workspace_id: str) -> WorkspaceAccessContext:
        if require_uuid(workspace_id, "workspace_id") != self.workspace_id:
            raise MigrationIdentifierConfusionError(
                "The worker was asked to open another MigrationWorkspace"
            )
        return WorkspaceAccessContext(
            project_id=self.business_project_id,
            workspace_id=self.workspace_id,
            data_version_id=self.data_version_id,
            migration_run_id=self.migration_run_id,
            recipe_application_id=self.recipe_application_id,
        )

    def resolve_workspace_access_context(
        self,
        workspace_id: str,
    ) -> WorkspaceAccessContext:
        return self.workspace_access_context(workspace_id)
