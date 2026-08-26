"""Authorize and coordinate commands for the Project business root."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from ...access import Actor, AuthorizationPolicy, Capability
from ...domain.project.models import (
    MigrationDataClassification,
    MigrationProject,
    MigrationProjectSummary,
    MigrationProjectStatus,
)
from ...domain.serialization import content_hash
from ...migration_foundation import (
    FaultInjector,
    require_revision,
    require_uuid,
    utc_now,
)
from .ports import MigrationProjectRepository


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
