"""Define bounded PostgreSQL Project catalogue values and failures."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProjectStatus,
)
from impodo.domain.shared.tenancy import OrganizationId


class ProjectCatalogueState(StrEnum):
    """Describe whether bounded control facts match proven data-plane state."""

    PENDING = "PENDING"
    READY = "READY"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class ProjectCatalogueError(RuntimeError):
    """Base application-facing Project catalogue failure."""


class ProjectCatalogueNotFoundError(ProjectCatalogueError):
    """Hide absent, cross-Organization, and undiscoverable Projects alike."""


class ProjectCatalogueAlreadyExistsError(ProjectCatalogueError):
    """Report that one Organization already owns the Project identity."""


class ProjectCatalogueRevisionConflictError(ProjectCatalogueError):
    """Report that a mutation did not target the current Project revision."""


class ProjectCatalogueStateConflictError(ProjectCatalogueError):
    """Report that a requested lifecycle transition is not currently valid."""


class ProjectCatalogueUnavailableError(ProjectCatalogueError):
    """Report a bounded PostgreSQL transport or retryable concurrency failure."""


class ProjectCatalogueStorageError(ProjectCatalogueError):
    """Report rejected or malformed storage behavior without database detail."""


@dataclass(frozen=True, slots=True)
class ProjectCatalogueRecord:
    """Hold only the bounded Project facts owned by the control plane."""

    organization_id: OrganizationId
    project_id: str
    display_name: str
    status: MigrationProjectStatus
    data_classification: MigrationDataClassification
    retention_days: int
    optimistic_revision: int
    catalogue_state: ProjectCatalogueState
    artifact_root_key: str | None
    current_receipt_hash: str | None
    created_at: datetime
    updated_at: datetime
    deletion_requested_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.project_id, "project_id")
        object.__setattr__(
            self,
            "display_name",
            required_text(self.display_name, "display_name", maximum=200),
        )
        object.__setattr__(self, "status", MigrationProjectStatus(self.status))
        object.__setattr__(
            self,
            "data_classification",
            MigrationDataClassification(self.data_classification),
        )
        object.__setattr__(
            self,
            "catalogue_state",
            ProjectCatalogueState(self.catalogue_state),
        )
        if not 1 <= self.retention_days <= 3_650:
            raise MigrationFoundationError("retention_days is invalid")
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise MigrationFoundationError("updated_at precedes created_at")
        self._validate_artifact_pointer()
        self._validate_deletion_state()

    def _validate_artifact_pointer(self) -> None:
        if self.artifact_root_key is None:
            artifact_root_key = None
        else:
            artifact_root_key = required_text(
                self.artifact_root_key,
                "artifact_root_key",
                maximum=500,
            )
            path = PurePosixPath(artifact_root_key)
            if path.is_absolute() or ".." in path.parts or "\\" in artifact_root_key:
                raise MigrationFoundationError("artifact_root_key is invalid")
            object.__setattr__(self, "artifact_root_key", artifact_root_key)
        if self.current_receipt_hash is not None:
            require_hash(self.current_receipt_hash, "current_receipt_hash")
        if self.catalogue_state is ProjectCatalogueState.READY and (
            artifact_root_key is None or self.current_receipt_hash is None
        ):
            raise MigrationFoundationError(
                "ready Project catalogue requires a proven artifact pointer"
            )

    def _validate_deletion_state(self) -> None:
        if self.deletion_requested_at is not None:
            require_aware(self.deletion_requested_at, "deletion_requested_at")
        if self.deleted_at is not None:
            require_aware(self.deleted_at, "deleted_at")
        if self.catalogue_state in {
            ProjectCatalogueState.DELETING,
            ProjectCatalogueState.DELETED,
        }:
            if self.deletion_requested_at is None:
                raise MigrationFoundationError(
                    "deleting Project catalogue requires a request time"
                )
        elif self.deletion_requested_at is not None:
            raise MigrationFoundationError(
                "active Project catalogue cannot retain a deletion request"
            )
        if self.catalogue_state is ProjectCatalogueState.DELETED:
            if self.deleted_at is None:
                raise MigrationFoundationError(
                    "deleted Project catalogue requires a completion time"
                )
        elif self.deleted_at is not None:
            raise MigrationFoundationError(
                "non-deleted Project catalogue cannot have a deletion time"
            )


_DEFAULT_VISIBLE_STATES = frozenset(
    {
        ProjectCatalogueState.PENDING,
        ProjectCatalogueState.READY,
        ProjectCatalogueState.REPAIR_REQUIRED,
        ProjectCatalogueState.DELETING,
    }
)


@dataclass(frozen=True, slots=True)
class ProjectCatalogueListQuery:
    """Define one deterministic, bounded visible-Project page."""

    statuses: frozenset[MigrationProjectStatus] | None = None
    catalogue_states: frozenset[ProjectCatalogueState] = field(
        default_factory=lambda: _DEFAULT_VISIBLE_STATES
    )
    page_size: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.statuses is not None:
            normalized_statuses = frozenset(
                MigrationProjectStatus(value) for value in self.statuses
            )
            if not normalized_statuses:
                raise MigrationFoundationError("statuses must not be empty")
            object.__setattr__(self, "statuses", normalized_statuses)
        normalized_states = frozenset(
            ProjectCatalogueState(value) for value in self.catalogue_states
        )
        if not normalized_states:
            raise MigrationFoundationError("catalogue_states must not be empty")
        object.__setattr__(self, "catalogue_states", normalized_states)
        if (
            not isinstance(self.page_size, int)
            or isinstance(self.page_size, bool)
            or not 1 <= self.page_size <= 100
        ):
            raise MigrationFoundationError("page_size is invalid")
        if self.cursor is not None:
            ProjectCatalogueCursor.decode(self.cursor)


@dataclass(frozen=True, slots=True)
class ProjectCatalogueCursor:
    """Encode the last `(updated_at DESC, project_id ASC)` ordering key."""

    updated_at: datetime
    project_id: str

    def __post_init__(self) -> None:
        require_aware(self.updated_at, "updated_at")
        require_uuid(self.project_id, "project_id")

    def encode(self) -> str:
        """Return the exact `pc1.<base64url>` cursor format without padding."""

        timestamp = (
            self.updated_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        payload = f"{timestamp}|{self.project_id}".encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"pc1.{encoded}"

    @classmethod
    def decode(cls, token: str) -> ProjectCatalogueCursor:
        """Parse one bounded cursor or reject it before querying storage."""

        if len(token) > 200 or not token.startswith("pc1."):
            raise MigrationFoundationError("Project catalogue cursor is invalid")
        encoded = token.removeprefix("pc1.")
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            ).decode("ascii")
            timestamp, project_id = payload.split("|", maxsplit=1)
            updated_at = datetime.fromisoformat(timestamp)
            return cls(updated_at=updated_at, project_id=project_id)
        except (UnicodeError, ValueError, binascii.Error) as error:
            raise MigrationFoundationError(
                "Project catalogue cursor is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class ProjectCataloguePage:
    """Return one stable visible page and its optional continuation cursor."""

    records: tuple[ProjectCatalogueRecord, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if self.next_cursor is not None:
            ProjectCatalogueCursor.decode(self.next_cursor)
