"""Define one Project run over an exact DataVersion and target context."""

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


class MigrationRunRepository(Protocol):
    def next_run_number(self, project_id: str) -> int: ...

    def create_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationRun: ...

    def get_migration_run(self, migration_run_id: str) -> MigrationRun: ...

    def list_migration_runs(self, project_id: str) -> tuple[MigrationRun, ...]: ...

    def save_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationRun: ...


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
