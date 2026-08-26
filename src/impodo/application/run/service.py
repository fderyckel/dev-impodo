"""Application service for authoritative Migration Run lifecycle commands."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from ...domain.run.models import MigrationRun, MigrationRunPurpose, MigrationRunState
from ...domain.serialization import content_hash
from impodo.domain.project.foundation import (
    FaultInjector,
    require_revision,
    require_uuid,
    utc_now,
)
from .ports import MigrationRunRepository


class MigrationRunService:
    """Authorize and coordinate one exact Project run."""

    def __init__(
        self,
        repository: MigrationRunRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def create(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_workspace_revision: int,
        data_version_id: str,
        purpose: str | MigrationRunPurpose,
        label: str,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> MigrationRun:
        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        now = utc_now()
        run = MigrationRun(
            migration_run_id=str(uuid4()),
            project_id=project_id,
            data_version_id=data_version_id,
            run_number=self.repository.next_run_number(project_id),
            purpose=MigrationRunPurpose(purpose),
            label=label,
            state=MigrationRunState.DRAFT,
            target_binding_id=None,
            cutover_selection_id=None,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        request_hash = content_hash(
            {
                "data_version_id": data_version_id,
                "label": run.label,
                "project_id": project_id,
                "purpose": run.purpose.value,
            }
        )
        return self.repository.create_migration_run(
            run,
            expected_workspace_revision=require_revision(
                expected_workspace_revision,
                "expected_workspace_revision",
            ),
            operation_id=operation_id or str(uuid4()),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

    def get(self, migration_run_id: str, *, actor: Actor) -> MigrationRun:
        self.authorization.require(actor, Capability.PROJECT_VIEW)
        run = self.repository.get_migration_run(
            require_uuid(migration_run_id, "migration_run_id")
        )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=run.project_id,
        )
        return run

    def list(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[MigrationRun, ...]:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.list_migration_runs(project_id)

    def rename(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        label: str,
    ) -> MigrationRun:
        self.authorization.require(actor, Capability.MIGRATION_RUN_EDIT)
        current = self.repository.get_migration_run(
            require_uuid(migration_run_id, "migration_run_id")
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_EDIT,
            project_id=current.project_id,
        )
        return self.repository.save_migration_run(
            replace(current, label=label, updated_at=utc_now()),
            expected_revision=require_revision(expected_revision),
            event_type="MIGRATION_RUN_RENAMED",
            actor=actor,
        )
