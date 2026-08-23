"""Define the clean Project-owned DataVersion aggregate root."""

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
    optional_text,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)


class DataVersionPurpose(StrEnum):
    AUTHORING = "AUTHORING"
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class DataVersionState(StrEnum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"


@dataclass(frozen=True, slots=True)
class DataVersion:
    """Identify one Project-owned source package independently of Recipes."""

    data_version_id: str
    project_id: str
    version_number: int
    parent_data_version_id: str | None
    purpose: DataVersionPurpose
    state: DataVersionState
    label: str
    export_as_of: str
    source_package_hash: str | None
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime
    frozen_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.data_version_id, "data_version_id")
        require_uuid(self.project_id, "project_id")
        if self.parent_data_version_id is not None:
            require_uuid(self.parent_data_version_id, "parent_data_version_id")
            if self.parent_data_version_id == self.data_version_id:
                raise MigrationFoundationError(
                    "A DataVersion cannot be its own parent"
                )
        if self.version_number < 1:
            raise MigrationFoundationError("version_number is invalid")
        object.__setattr__(self, "purpose", DataVersionPurpose(self.purpose))
        object.__setattr__(self, "state", DataVersionState(self.state))
        object.__setattr__(
            self,
            "label",
            required_text(self.label, "label", maximum=200),
        )
        object.__setattr__(
            self,
            "export_as_of",
            optional_text(self.export_as_of, "export_as_of", maximum=100),
        )
        if self.source_package_hash is not None:
            require_hash(self.source_package_hash, "source_package_hash")
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.frozen_at is not None:
            require_aware(self.frozen_at, "frozen_at")
        if self.state is DataVersionState.FROZEN and (
            self.source_package_hash is None or self.frozen_at is None
        ):
            raise MigrationFoundationError(
                "A frozen DataVersion requires package identity and freeze time"
            )


class DataVersionRepository(Protocol):
    def next_data_version_number(self, project_id: str) -> int: ...

    def create_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersion: ...

    def get_data_version(self, data_version_id: str) -> DataVersion: ...

    def list_data_versions(self, project_id: str) -> tuple[DataVersion, ...]: ...

    def save_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> DataVersion: ...


class DataVersionService:
    """Authorize and coordinate Project-owned DataVersion commands."""

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
