"""Common read-only issue projection for the six Authoring stages.

Authoritative stage evidence remains in its owning service.  This module only
normalizes current findings for consistent browser messages and never stores a
second mutable issue database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class WorkflowIssueSeverity(StrEnum):
    """User-facing urgency shared by every stage projection."""

    MUST_FIX = "MUST_FIX"
    REVIEW = "REVIEW"


class WorkflowIssueState(StrEnum):
    """Freshness state derived from the authoritative owning evidence."""

    CURRENT = "CURRENT"
    STALE = "STALE"
    RESOLVED = "RESOLVED"


class WorkflowIssueOrigin(StrEnum):
    """Business origin used to route a correction to the right owner."""

    SOURCE_DATA = "SOURCE_DATA"
    INCOMING_RELATIONSHIP = "INCOMING_RELATIONSHIP"
    ODOO_SCHEMA = "ODOO_SCHEMA"
    ODOO_DATA = "ODOO_DATA"
    IMPODO_ACTION = "IMPODO_ACTION"


@dataclass(frozen=True, slots=True)
class WorkflowIssueProjection:
    """One actionable issue recalculated from current stage evidence."""

    code: str
    severity: WorkflowIssueSeverity
    state: WorkflowIssueState
    cause: str
    origin: WorkflowIssueOrigin
    owner: str
    owning_stage: str
    correction_label: str
    correction_route: str
    preserved_work: str
    recheck: str
    evidence_revision: str
    affected_record_types: tuple[str, ...] = ()
    affected_fields: tuple[str, ...] = ()
    affected_record_count: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowIssueSummary:
    """Sorted current issues and their navigation-safe counts."""

    issues: tuple[WorkflowIssueProjection, ...]
    must_fix_count: int
    review_count: int


def summarize_workflow_issues(
    issues: Iterable[WorkflowIssueProjection],
) -> WorkflowIssueSummary:
    """Keep only current findings and place blockers before review items."""

    current = tuple(
        sorted(
            (
                issue
                for issue in issues
                if issue.state is WorkflowIssueState.CURRENT
            ),
            key=lambda issue: (
                issue.severity is not WorkflowIssueSeverity.MUST_FIX,
                issue.owning_stage,
                issue.cause.casefold(),
                issue.code,
            ),
        )
    )
    return WorkflowIssueSummary(
        issues=current,
        must_fix_count=sum(
            issue.severity is WorkflowIssueSeverity.MUST_FIX
            for issue in current
        ),
        review_count=sum(
            issue.severity is WorkflowIssueSeverity.REVIEW
            for issue in current
        ),
    )


__all__ = [
    "WorkflowIssueOrigin",
    "WorkflowIssueProjection",
    "WorkflowIssueSeverity",
    "WorkflowIssueState",
    "WorkflowIssueSummary",
    "summarize_workflow_issues",
]
