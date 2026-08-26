"""Define the Project business identity, governance state, and summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_aware,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)


class MigrationProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class MigrationDataClassification(StrEnum):
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


@dataclass(frozen=True, slots=True)
class MigrationProject:
    """Own one governed legacy-to-Odoo migration effort."""

    project_id: str
    display_name: str
    migration_purpose: str
    source_system_identity: str
    data_classification: MigrationDataClassification
    retention_days: int
    status: MigrationProjectStatus
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.project_id, "project_id")
        object.__setattr__(
            self,
            "display_name",
            required_text(self.display_name, "display_name", maximum=200),
        )
        object.__setattr__(
            self,
            "migration_purpose",
            required_text(
                self.migration_purpose,
                "migration_purpose",
                maximum=2_000,
            ),
        )
        object.__setattr__(
            self,
            "source_system_identity",
            required_text(
                self.source_system_identity,
                "source_system_identity",
                maximum=500,
            ),
        )
        object.__setattr__(
            self,
            "data_classification",
            MigrationDataClassification(self.data_classification),
        )
        object.__setattr__(self, "status", MigrationProjectStatus(self.status))
        if not 1 <= self.retention_days <= 3_650:
            raise MigrationFoundationError("retention_days is invalid")
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.closed_at is not None:
            require_aware(self.closed_at, "closed_at")
        if self.archived_at is not None:
            require_aware(self.archived_at, "archived_at")


@dataclass(frozen=True, slots=True)
class MigrationProjectSummary:
    """Return one bounded Project-list projection from the registry."""

    project_id: str
    display_name: str
    status: MigrationProjectStatus
    optimistic_revision: int
    data_version_count: int
    run_count: int
    workspace_count: int
    recipe_count: int
    updated_at: datetime
