"""Define isolated MigrationWorkspace identity, lifecycle, and setup state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.domain.project.foundation import (
    require_aware,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)


class MigrationWorkspaceState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class MigrationWorkspaceSetupState(StrEnum):
    """Track whether the contained workspace may start governed authoring."""

    DRAFT = "DRAFT"
    READY = "READY"


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
    setup_state: MigrationWorkspaceSetupState = MigrationWorkspaceSetupState.DRAFT
    setup_completed_at: datetime | None = None
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
        object.__setattr__(
            self,
            "setup_state",
            MigrationWorkspaceSetupState(self.setup_state),
        )
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.closed_at is not None:
            require_aware(self.closed_at, "closed_at")
        if self.setup_completed_at is not None:
            require_aware(self.setup_completed_at, "setup_completed_at")
        if (
            self.setup_state is MigrationWorkspaceSetupState.READY
            and self.setup_completed_at is None
        ):
            raise ValueError("A ready MigrationWorkspace requires a setup time")
        if (
            self.setup_state is MigrationWorkspaceSetupState.DRAFT
            and self.setup_completed_at is not None
        ):
            raise ValueError("A draft MigrationWorkspace cannot have a setup time")

