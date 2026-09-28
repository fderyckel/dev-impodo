"""Declare the persistence port consumed by workspace lifecycle commands."""

from __future__ import annotations

from typing import Protocol

from impodo.domain.shared.access import Actor
from ...domain.workspace.models import MigrationWorkspace
from impodo.domain.project.foundation import FaultInjector


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
    ) -> MigrationWorkspace:
        """Persist the root requested by ``MigrationWorkspaceService.create``.

        Implementations own atomic idempotency, optimistic parent revision,
        and lifecycle-event persistence; this port carries no authorization
        policy and must not invent workspace lineage.
        """
        ...

    def get_migration_workspace(self, workspace_id: str) -> MigrationWorkspace:
        """Read one root for lifecycle commands after service authorization."""
        ...

    def list_migration_workspaces(
        self,
        migration_run_id: str,
    ) -> tuple[MigrationWorkspace, ...]:
        """List roots for a run; retained for run-level application callers."""
        ...

    def list_project_migration_workspaces(
        self,
        project_id: str,
    ) -> tuple[MigrationWorkspace, ...]:
        """List roots for ``MigrationWorkspaceService.list_for_project``."""
        ...

    def save_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationWorkspace:
        """Save a lifecycle transition requested by the workspace service."""
        ...
