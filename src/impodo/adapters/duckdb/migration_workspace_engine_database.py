"""Resolve the current mapping engine through clean MigrationWorkspace identity."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ...migration_foundation import (
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
    require_uuid,
)
from .database import DuckDbWorkspaceDatabase
from .migration_foundation_database import MigrationFoundationDatabase


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
        """Resolve one MigrationWorkspace engine directory."""

        try:
            workspace_id = str(UUID(workspace_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise MigrationNotFoundError("Invalid workspace_id") from error
        with self.foundation.connect(self.foundation.registry_path) as connection:
            row = connection.execute(
                "SELECT project_id FROM migration_workspace WHERE workspace_id = ?",
                [workspace_id],
            ).fetchone()
        if row is None:
            raise MigrationNotFoundError("MigrationWorkspace not found")
        return self.foundation.workspace_directory(str(row[0]), workspace_id)


class FixedMigrationWorkspaceEngineDatabase(DuckDbWorkspaceDatabase):
    """Resolve one authorized workspace without opening the shared registry."""

    def __init__(
        self,
        root: str | Path,
        *,
        project_id: str,
        workspace_id: str,
        lock_wait_timeout_seconds: float = 0.0,
    ) -> None:
        super().__init__(
            root,
            lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        )
        self.business_project_id = require_uuid(project_id, "project_id")
        self.workspace_id = require_uuid(workspace_id, "workspace_id")

    def workspace_directory(self, workspace_id: str) -> Path:
        if require_uuid(workspace_id, "workspace_id") != self.workspace_id:
            raise MigrationIdentifierConfusionError(
                "The worker was asked to open another MigrationWorkspace"
            )
        return (
            self.root
            / "projects"
            / self.business_project_id
            / "workspaces"
            / self.workspace_id
        )
