"""Define one Project run over an exact DataVersion and target context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ...migration_foundation import (
    MigrationFoundationError,
    require_aware,
    require_revision,
    require_uuid,
    required_text,
)


class MigrationRunPurpose(StrEnum):
    AUTHORING = "AUTHORING"
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class MigrationRunState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    INCOMPLETE = "INCOMPLETE"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class MigrationRun:
    """Coordinate one use of one DataVersion against one target identity."""

    migration_run_id: str
    project_id: str
    data_version_id: str
    run_number: int
    purpose: MigrationRunPurpose
    label: str
    state: MigrationRunState
    target_binding_id: str | None
    cutover_selection_id: str | None
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.migration_run_id, "migration_run_id"),
            (self.project_id, "project_id"),
            (self.data_version_id, "data_version_id"),
        ):
            require_uuid(value, name)
        for value, name in (
            (self.target_binding_id, "target_binding_id"),
            (self.cutover_selection_id, "cutover_selection_id"),
        ):
            if value is not None:
                require_uuid(value, name)
        if self.run_number < 1:
            raise MigrationFoundationError("run_number is invalid")
        object.__setattr__(self, "purpose", MigrationRunPurpose(self.purpose))
        object.__setattr__(self, "state", MigrationRunState(self.state))
        object.__setattr__(
            self,
            "label",
            required_text(self.label, "label", maximum=200),
        )
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.closed_at is not None:
            require_aware(self.closed_at, "closed_at")
