"""Count current Stage 3 findings without exposing protected match values."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.application.workspace.issues import WorkflowIssueSeverity
from impodo.domain.matching_order import (
    MatchingIdentityNotCheckableReason,
    MatchingIdentityResult,
    MatchingOrderCheck,
)
from impodo.domain.relationship_health import RelationshipHealthResult


@dataclass(frozen=True, slots=True)
class MatchingHealthIssueCounts:
    """Navigation-safe counts derived from one current matching check."""

    must_fix: int = 0
    review: int = 0


def identity_health_issue_severity(
    result: MatchingIdentityResult,
) -> WorkflowIssueSeverity | None:
    """Classify one identity aggregate without changing mapping gates."""

    if result.checked:
        return (
            WorkflowIssueSeverity.MUST_FIX
            if result.blocked_count
            else None
        )
    if (
        result.not_checkable_reason
        is MatchingIdentityNotCheckableReason.ODOO_PINNED_SELECTION
    ):
        return None
    return WorkflowIssueSeverity.REVIEW


def relationship_health_issue_severity(
    result: RelationshipHealthResult,
) -> WorkflowIssueSeverity | None:
    """Classify one relationship aggregate without changing readiness."""

    if result.checked:
        return (
            WorkflowIssueSeverity.MUST_FIX
            if result.blocked_count
            else None
        )
    return WorkflowIssueSeverity.REVIEW


def matching_health_issue_counts(
    check: MatchingOrderCheck,
) -> MatchingHealthIssueCounts:
    """Return issue-card counts from one bounded current Stage 3 check."""

    severities = (
        *(
            identity_health_issue_severity(result)
            for result in check.identity_results
        ),
        *(
            relationship_health_issue_severity(result)
            for result in check.relationship_health_results
        ),
    )
    return MatchingHealthIssueCounts(
        must_fix=sum(
            severity is WorkflowIssueSeverity.MUST_FIX
            for severity in severities
        ),
        review=sum(
            severity is WorkflowIssueSeverity.REVIEW
            for severity in severities
        ),
    )


__all__ = [
    "MatchingHealthIssueCounts",
    "identity_health_issue_severity",
    "matching_health_issue_counts",
    "relationship_health_issue_severity",
]
