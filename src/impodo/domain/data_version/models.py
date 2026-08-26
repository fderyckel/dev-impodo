"""Define the Project-owned Data version identity and accepted-state rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.domain.project.foundation import (
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

