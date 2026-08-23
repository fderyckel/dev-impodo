"""Persist the mapping engine state contained by one MigrationWorkspace."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil

from ...access import Actor
from ...migration_foundation import MigrationConflictError
from ...migration_workspaces import MigrationWorkspaceState
from ...workspace_state import (
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
)
from .migration_foundation_repository import MigrationFoundationRepository
from .migration_workspace_engine_database import MigrationWorkspaceEngineDatabase
from .workspace_state_repository import WorkspaceStateRepository
from .repository import DuckDbRepository


class MigrationWorkspaceStateRepository(WorkspaceStateRepository):
    """Persist mapping-engine state for workspaces resolved by the registry."""

    _ENGINE_DIRECTORIES = (
        "inbox",
        "staging",
        "snapshots",
        "protected",
        "reports",
        "audit",
    )

    def __init__(
        self,
        database: MigrationWorkspaceEngineDatabase,
        foundation: MigrationFoundationRepository,
    ) -> None:
        DuckDbRepository.__init__(self, database)
        self.foundation = foundation

    def create(
        self,
        project: WorkspaceState,
        *,
        recipe_id: str,
        data_version_id: str,
        creation_request_id: str | None = None,
        creation_request_hash: str | None = None,
        actor: Actor,
    ) -> None:
        raise WorkspaceStateError(
            "A MigrationWorkspace engine cannot create a Recipe or Project root"
        )

    def create_unlinked(self, project: WorkspaceState, *, actor: Actor) -> None:
        """Initialize only the engine state for an existing clean workspace."""

        workspace = self.foundation.get_migration_workspace(project.project_id)
        if workspace.state is not MigrationWorkspaceState.OPEN:
            raise WorkspaceStateError("A closed MigrationWorkspace cannot be initialized")
        directory = self.workspace_directory(project.project_id)
        database_path = directory / "project.duckdb"
        if database_path.is_file():
            current = self.get(project.project_id)
            if current != project:
                raise MigrationConflictError(
                    "MigrationWorkspace engine was already initialized differently"
                )
            return
        created: list[Path] = []
        try:
            for name in self._ENGINE_DIRECTORIES:
                child = directory / name
                child.mkdir(exist_ok=False)
                created.append(child)
            (directory / "protected").chmod(0o700)
            with self._connect(database_path) as connection:
                self._initialize_workspace_database(connection)
                self._insert_project(connection, project)
                self._insert_audit(
                    connection,
                    project,
                    event_type="MIGRATION_WORKSPACE_ENGINE_CREATED",
                    detail="",
                    actor=actor,
                )
        except Exception:
            database_path.unlink(missing_ok=True)
            for child in reversed(created):
                if child.is_dir():
                    shutil.rmtree(child)
            raise

    def discard_unlinked(self, project_id: str) -> None:
        raise WorkspaceStateError(
            "MigrationWorkspace lifecycle is owned by the clean Project registry"
        )

    def get(self, project_id: str) -> WorkspaceState:
        self.foundation.get_migration_workspace(project_id)
        database_path = self.workspace_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace engine not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
        return self._get_project_unresolved(project_id)

    def assert_workspace_mutable(self, project_id: str) -> None:
        workspace = self.foundation.get_migration_workspace(project_id)
        if workspace.state is not MigrationWorkspaceState.OPEN:
            raise WorkspaceStateError("This MigrationWorkspace is closed and read-only")

    def record_credential_removal_receipt(
        self,
        *,
        receipt_hash: str,
        project_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        self.record_credential_event(
            project_id,
            event_type="TARGET_CREDENTIAL_REMOVED",
            detail=(
                f"receipt={receipt_hash};role={role};reason={reason};"
                f"target={connection_target_hash};binding="
                f"{credential_binding_hash or ''};storage={storage_class};"
                f"removed_at={removed_at.isoformat()}"
            ),
            actor=actor,
        )

