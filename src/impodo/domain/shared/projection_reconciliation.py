"""Define bounded receipt-projection reconciliation facts and failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.domain.shared.coordination import CrossStoreOperationState

PROJECTION_RECONCILIATION_MAXIMUM_PAGE_SIZE = 100


class ProjectionReconciliationDisposition(StrEnum):
    """Classify one receipt projection without inventing business outcomes."""

    AWAITING_DATA_COMMIT = "AWAITING_DATA_COMMIT"
    MISSING_PROJECTION = "MISSING_PROJECTION"
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"
    NOT_REBUILDABLE = "NOT_REBUILDABLE"


class ProjectionRebuildNotPermittedError(RuntimeError):
    """Reject rebuilding anything except a proven missing receipt projection."""


class ProjectionReconciliationCursorError(ValueError):
    """Reject a malformed or cross-scope administrative scan cursor."""


class ProjectionReconciliationNotFoundError(LookupError):
    """Hide absent, cross-Organization, and undiscoverable targets alike."""


@dataclass(frozen=True, slots=True)
class ProjectionReconciliationQuery:
    """Bound one administrative scan page and its opaque continuation token."""

    page_size: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= PROJECTION_RECONCILIATION_MAXIMUM_PAGE_SIZE:
            raise ValueError(
                "projection reconciliation page_size must be between 1 and "
                f"{PROJECTION_RECONCILIATION_MAXIMUM_PAGE_SIZE}"
            )
        if self.cursor is not None:
            cursor = self.cursor.strip()
            if not cursor or len(cursor) > 2_000:
                raise ValueError("projection reconciliation cursor is invalid")
            object.__setattr__(self, "cursor", cursor)


@dataclass(frozen=True, slots=True)
class ProjectionReconciliationItem:
    """Expose verified bounded receipt facts for one durable operation."""

    operation_id: str
    operation_kind: str
    requested_at: datetime
    state: CrossStoreOperationState
    disposition: ProjectionReconciliationDisposition
    data_plane_receipt_hash: str | None
    projection_hash: str | None
    projection_revision: int | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.operation_kind, "operation_kind"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        object.__setattr__(self, "state", CrossStoreOperationState(self.state))
        object.__setattr__(
            self,
            "disposition",
            ProjectionReconciliationDisposition(self.disposition),
        )
        if (self.projection_hash is None) != (self.projection_revision is None):
            raise ValueError("projection hash and revision must be present together")
        if self.projection_revision is not None and self.projection_revision < 1:
            raise ValueError("projection_revision must be positive")


@dataclass(frozen=True, slots=True)
class ProjectionReconciliationPage:
    """Return one deterministic page of Organization-scoped operation facts."""

    items: tuple[ProjectionReconciliationItem, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if len(self.items) > PROJECTION_RECONCILIATION_MAXIMUM_PAGE_SIZE:
            raise ValueError("projection reconciliation page is too large")
        if self.next_cursor is not None and not self.next_cursor.strip():
            raise ValueError("next_cursor must not be blank")


__all__ = [
    "PROJECTION_RECONCILIATION_MAXIMUM_PAGE_SIZE",
    "ProjectionRebuildNotPermittedError",
    "ProjectionReconciliationCursorError",
    "ProjectionReconciliationDisposition",
    "ProjectionReconciliationItem",
    "ProjectionReconciliationNotFoundError",
    "ProjectionReconciliationPage",
    "ProjectionReconciliationQuery",
]
