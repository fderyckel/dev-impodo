"""Authorize and coordinate Project-owned Data version commands."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from ...domain.data_version.models import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
)
from ...domain.serialization import content_hash
from impodo.domain.project.foundation import (
    FaultInjector,
    MigrationFoundationError,
    require_revision,
    require_uuid,
    utc_now,
)
from .ports import DataVersionRepository


class DataVersionService:
    """Authorize and coordinate Project-owned Data version commands."""

    def __init__(
        self,
        repository: DataVersionRepository,
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
        purpose: str | DataVersionPurpose,
        label: str,
        export_as_of: str = "",
        parent_data_version_id: str | None = None,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> DataVersion:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.DATA_VERSION_CREATE,
            project_id=project_id,
        )
        now = utc_now()
        data_version = DataVersion(
            data_version_id=str(uuid4()),
            project_id=project_id,
            version_number=self.repository.next_data_version_number(project_id),
            parent_data_version_id=(
                require_uuid(parent_data_version_id, "parent_data_version_id")
                if parent_data_version_id is not None
                else None
            ),
            purpose=DataVersionPurpose(purpose),
            state=DataVersionState.DRAFT,
            label=label,
            export_as_of=export_as_of,
            source_package_hash=None,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        request_hash = content_hash(
            {
                "export_as_of": data_version.export_as_of,
                "label": data_version.label,
                "parent_data_version_id": data_version.parent_data_version_id,
                "project_id": project_id,
                "purpose": data_version.purpose.value,
            }
        )
        return self.repository.create_data_version(
            data_version,
            expected_workspace_revision=require_revision(
                expected_workspace_revision,
                "expected_workspace_revision",
            ),
            operation_id=operation_id or str(uuid4()),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

    def get(self, data_version_id: str, *, actor: Actor) -> DataVersion:
        data_version_id = require_uuid(data_version_id, "data_version_id")
        self.authorization.require(actor, Capability.PROJECT_VIEW)
        data_version = self.repository.get_data_version(data_version_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=data_version.project_id,
        )
        return data_version

    def list(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[DataVersion, ...]:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.list_data_versions(project_id)

    def rename(
        self,
        data_version_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        label: str,
    ) -> DataVersion:
        self.authorization.require(actor, Capability.DATA_VERSION_EDIT)
        current = self.repository.get_data_version(
            require_uuid(data_version_id, "data_version_id")
        )
        self.authorization.require(
            actor,
            Capability.DATA_VERSION_EDIT,
            project_id=current.project_id,
        )
        if current.state is DataVersionState.FROZEN:
            raise MigrationFoundationError("A frozen DataVersion is immutable")
        return self.repository.save_data_version(
            replace(current, label=label, updated_at=utc_now()),
            expected_revision=require_revision(expected_revision),
            event_type="DATA_VERSION_RENAMED",
            actor=actor,
        )
