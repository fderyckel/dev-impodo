"""Declare the persistence port consumed by workspace lifecycle commands."""

from __future__ import annotations

from typing import Protocol

from ...access import Actor
from ...domain.workspace.models import MigrationWorkspace
from ...migration_foundation import FaultInjector


class MigrationWorkspaceRepository(Protocol):
    """Persist workspace roots without exposing a storage implementation."""

    def create_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationWorkspace: ...

    def get_migration_workspace(self, workspace_id: str) -> MigrationWorkspace: ...

    def list_migration_workspaces(
        self,
        migration_run_id: str,
    ) -> tuple[MigrationWorkspace, ...]: ...

    def list_project_migration_workspaces(
        self,
        project_id: str,
    ) -> tuple[MigrationWorkspace, ...]: ...

    def save_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationWorkspace: ...
