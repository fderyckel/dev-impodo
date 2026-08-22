"""Define the clean Project business root introduced by Phase M1."""

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


class MigrationProjectRepository(Protocol):
    def create_project(
        self,
        project: MigrationProject,
        *,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationProject: ...

    def get_project(self, project_id: str) -> MigrationProject: ...

    def list_project_summaries(self) -> tuple[MigrationProjectSummary, ...]: ...

    def save_project(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationProject: ...


class MigrationProjectService:
    """Authorize and coordinate commands for the Project business root."""

    def __init__(
        self,
        repository: MigrationProjectRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def create(
        self,
        *,
        actor: Actor,
        display_name: str,
        migration_purpose: str,
        source_system_identity: str,
        data_classification: str | MigrationDataClassification = (
            MigrationDataClassification.INTERNAL
        ),
        retention_days: int = 365,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> MigrationProject:
        self.authorization.require(actor, Capability.PROJECT_CREATE)
        now = utc_now()
        project = MigrationProject(
            project_id=str(uuid4()),
            display_name=display_name,
            migration_purpose=migration_purpose,
            source_system_identity=source_system_identity,
            data_classification=MigrationDataClassification(data_classification),
            retention_days=retention_days,
            status=MigrationProjectStatus.DRAFT,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        request_hash = content_hash(
            {
                "data_classification": project.data_classification.value,
                "display_name": project.display_name,
                "migration_purpose": project.migration_purpose,
                "retention_days": project.retention_days,
                "source_system_identity": project.source_system_identity,
            }
        )
        return self.repository.create_project(
            project,
            operation_id=operation_id or str(uuid4()),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

    def get(self, project_id: str, *, actor: Actor) -> MigrationProject:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.get_project(project_id)

    def list(self, *, actor: Actor) -> tuple[MigrationProjectSummary, ...]:
        self.authorization.require(actor, Capability.PROJECT_VIEW)
        return self.repository.list_project_summaries()

    def rename(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        display_name: str,
    ) -> MigrationProject:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_EDIT,
            project_id=project_id,
        )
        current = self.repository.get_project(project_id)
        updated = replace(
            current,
            display_name=display_name,
            updated_at=utc_now(),
        )
        return self.repository.save_project(
            updated,
            expected_revision=require_revision(expected_revision),
            event_type="MIGRATION_PROJECT_RENAMED",
            actor=actor,
        )
