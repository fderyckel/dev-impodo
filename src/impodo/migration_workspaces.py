"""Define the isolated technical workspace root introduced by Phase M1."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from .access import Actor, AuthorizationPolicy, Capability
from .domain.serialization import content_hash
from .migration_foundation import (
    FaultInjector,
    require_aware,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)


class MigrationWorkspaceState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class MigrationWorkspace:
    """Own one isolated mapping and execution work area inside a run."""

    workspace_id: str
    project_id: str
    data_version_id: str
    migration_run_id: str
    recipe_application_id: str | None
    display_name: str
    state: MigrationWorkspaceState
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.workspace_id, "workspace_id"),
            (self.project_id, "project_id"),
            (self.data_version_id, "data_version_id"),
            (self.migration_run_id, "migration_run_id"),
        ):
            require_uuid(value, name)
        if self.recipe_application_id is not None:
            require_uuid(self.recipe_application_id, "recipe_application_id")
        object.__setattr__(
            self,
            "display_name",
            required_text(self.display_name, "display_name", maximum=200),
        )
        object.__setattr__(self, "state", MigrationWorkspaceState(self.state))
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.closed_at is not None:
            require_aware(self.closed_at, "closed_at")


class MigrationWorkspaceRepository(Protocol):
    def create_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_project_revision: int,
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

    def save_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationWorkspace: ...


class MigrationWorkspaceService:
    """Authorize and coordinate isolated workspace commands."""

    def __init__(
        self,
        repository: MigrationWorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def create(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_project_revision: int,
        data_version_id: str,
        migration_run_id: str,
        display_name: str,
        recipe_application_id: str | None = None,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> MigrationWorkspace:
        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_CREATE,
            project_id=project_id,
        )
        application_id = (
            require_uuid(recipe_application_id, "recipe_application_id")
            if recipe_application_id is not None
            else None
        )
        now = utc_now()
        workspace = MigrationWorkspace(
            workspace_id=str(uuid4()),
            project_id=project_id,
            data_version_id=data_version_id,
            migration_run_id=migration_run_id,
            recipe_application_id=application_id,
            display_name=display_name,
            state=MigrationWorkspaceState.OPEN,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        request_hash = content_hash(
            {
                "data_version_id": data_version_id,
                "display_name": workspace.display_name,
                "migration_run_id": migration_run_id,
                "project_id": project_id,
                "recipe_application_id": application_id,
            }
        )
        return self.repository.create_migration_workspace(
            workspace,
            expected_project_revision=require_revision(
                expected_project_revision,
                "expected_project_revision",
            ),
            operation_id=operation_id or str(uuid4()),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

    def close(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> MigrationWorkspace:
        self.authorization.require(actor, Capability.MIGRATION_WORKSPACE_EDIT)
        current = self.repository.get_migration_workspace(
            require_uuid(workspace_id, "workspace_id")
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_EDIT,
            project_id=current.project_id,
        )
        now = utc_now()
        return self.repository.save_migration_workspace(
            replace(
                current,
                state=MigrationWorkspaceState.CLOSED,
                closed_at=now,
                updated_at=now,
            ),
            expected_revision=require_revision(expected_revision),
            event_type="MIGRATION_WORKSPACE_CLOSED",
            actor=actor,
        )
