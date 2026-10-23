"""Portable Project retention and deletion contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.tenancy import OrganizationId

DEFAULT_INACTIVITY_RETENTION_DAYS = 90


class RetentionMode(StrEnum):
    """Customer-selectable Project data retention behavior."""

    KEEP_UNTIL_MANUAL_DELETION = "KEEP_UNTIL_MANUAL_DELETION"
    DELETE_AFTER_VERIFIED_ODOO_LOAD = "DELETE_AFTER_VERIFIED_ODOO_LOAD"
    DELETE_AFTER_INACTIVITY = "DELETE_AFTER_INACTIVITY"


class DeletionResultStatus(StrEnum):
    """Terminal result of one deletion request."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ProjectRetentionSelection:
    """Versioned retention choice made with an explicit warning."""

    organization_id: OrganizationId
    project_id: str
    mode: RetentionMode
    revision: int
    selected_by: ActorIdentity
    selected_at: datetime
    warning_revision: str
    inactivity_days: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", RetentionMode(self.mode))
        object.__setattr__(
            self,
            "project_id",
            _required_text(self.project_id, "project_id", maximum=200),
        )
        object.__setattr__(
            self,
            "warning_revision",
            _required_text(
                self.warning_revision,
                "warning_revision",
                maximum=200,
            ),
        )
        if self.revision < 1:
            raise ValueError("revision must be positive")
        _aware(self.selected_at, "selected_at")
        if self.mode is RetentionMode.DELETE_AFTER_INACTIVITY:
            if self.inactivity_days is None or self.inactivity_days < 1:
                raise ValueError(
                    "DELETE_AFTER_INACTIVITY requires positive inactivity_days"
                )
        elif self.inactivity_days is not None:
            raise ValueError("inactivity_days applies only to inactivity retention")

    @classmethod
    def default_for_project(
        cls,
        *,
        organization_id: OrganizationId,
        project_id: str,
        selected_by: ActorIdentity,
        selected_at: datetime,
        warning_revision: str,
    ) -> "ProjectRetentionSelection":
        """Create the server default of deletion after 90 inactive days."""

        return cls(
            organization_id=organization_id,
            project_id=project_id,
            mode=RetentionMode.DELETE_AFTER_INACTIVITY,
            revision=1,
            selected_by=selected_by,
            selected_at=selected_at,
            warning_revision=warning_revision,
            inactivity_days=DEFAULT_INACTIVITY_RETENTION_DAYS,
        )


@dataclass(frozen=True, slots=True)
class ProjectDeletionRequest:
    """Confirmed request to remove all retained customer Project data."""

    deletion_request_id: str
    organization_id: OrganizationId
    project_id: str
    retention_revision: int
    confirmation_hash: str
    requested_by: ActorIdentity
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in ("deletion_request_id", "project_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=200),
            )
        if self.retention_revision < 1:
            raise ValueError("retention_revision must be positive")
        _sha256_hash(self.confirmation_hash, "confirmation_hash")
        _aware(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class ProjectDeletionResult:
    """Bounded deletion outcome; it contains no customer values or storage keys."""

    deletion_request_id: str
    organization_id: OrganizationId
    project_id: str
    status: DeletionResultStatus
    result_code: str
    completed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DeletionResultStatus(self.status))
        for name in ("deletion_request_id", "project_id", "result_code"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=200),
            )
        _aware(self.completed_at, "completed_at")


@dataclass(frozen=True, slots=True)
class MinimalDeletionReceipt:
    """Minimal non-customer evidence retained after successful deletion."""

    deletion_receipt_id: str
    deletion_request_id: str
    organization_id: OrganizationId
    project_id: str
    policy_revision: str
    completed_by: ActorIdentity
    completed_at: datetime
    customer_data_retained: bool = False

    def __post_init__(self) -> None:
        for name in (
            "deletion_receipt_id",
            "deletion_request_id",
            "project_id",
            "policy_revision",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=200),
            )
        _aware(self.completed_at, "completed_at")
        if self.customer_data_retained:
            raise ValueError("a deletion receipt cannot retain customer data")


def _required_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


def _sha256_hash(value: str, name: str) -> str:
    digest = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(digest) != 64:
        raise ValueError(f"{name} must use sha256:<64 hex characters>")
    try:
        int(digest, 16)
    except ValueError as error:
        raise ValueError(f"{name} must use sha256:<64 hex characters>") from error
    return value


def _aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
