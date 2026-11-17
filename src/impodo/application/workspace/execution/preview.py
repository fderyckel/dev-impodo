"""Full execution query values, independent of journal and write coordination."""

from __future__ import annotations

from dataclasses import dataclass, field

from impodo.domain.execution.batch_policy import validated_create_batch_rows
from impodo.domain.execution.models import ExecutionRun, ExecutionRunStatus
from impodo.domain.execution.odoo_scope import OdooApiScope
from impodo.domain.execution_snapshot import ExecutionSnapshot

from .business_review import BusinessIntentReview
from .navigation import ExecutionNavigationState
from .review_models import (
    ExecutionReviewBlockerSummary,
    ExecutionReviewDataset,
    ExecutionReviewDependencySummary,
)
from .review_queries import (
    execution_blocker_summary,
    execution_dataset_previews,
    execution_dependency_summary,
)
from .validation_messages import NO_WRITE_ROWS_MESSAGE, execution_scope_error_code


@dataclass(frozen=True, slots=True)
class ExecutionPreview:
    snapshot: ExecutionSnapshot
    datasets: tuple[ExecutionReviewDataset, ...]
    current_run: ExecutionRun | None
    api_scope: OdooApiScope
    deferred_create_count: int
    scope_error: str = ""
    scope_error_code: str = ""
    credential_refresh_required: bool = False
    dependency_summary: ExecutionReviewDependencySummary = field(
        default_factory=ExecutionReviewDependencySummary
    )
    blocker_summary: ExecutionReviewBlockerSummary = field(
        default_factory=ExecutionReviewBlockerSummary
    )
    prior_interrupted_run: ExecutionRun | None = None
    business_intent_assessment_hash: str = ""
    business_intent: BusinessIntentReview | None = None

    @property
    def can_load(self) -> bool:
        """Say whether the reviewed snapshot is eligible to begin a load.

        Used by web confirmation routes and ``ExecutionService.execute`` as a
        pre-check. The service repeats full identity and snapshot validation
        before starting the durable journal.
        """
        return (
            self.snapshot.write_count > 0
            and int(self.snapshot.counts.get("BLOCKED", 0)) == 0
            and int(self.snapshot.counts.get("AMBIGUOUS", 0)) == 0
            and self.current_run is None
            and not self.scope_error
        )

    @property
    def can_complete_without_load(self) -> bool:
        """Say whether a no-write snapshot may be recorded as complete.

        Used by the completion route and ``complete_no_changes``. It excludes
        blocked, ambiguous, or stale-target evidence even though no Odoo write
        is planned.
        """
        return (
            self.snapshot.write_count == 0
            and int(self.snapshot.counts.get("BLOCKED", 0)) == 0
            and int(self.snapshot.counts.get("AMBIGUOUS", 0)) == 0
            and self.scope_error in {"", NO_WRITE_ROWS_MESSAGE}
            and (
                self.current_run is None
                or (
                    self.current_run.status is ExecutionRunStatus.COMPLETED
                    and self.current_run.total_count == 0
                )
            )
        )


@dataclass(frozen=True, slots=True)
class ExecutionNavigationPreview:
    """Bounded load-stage state that never materializes execution rows."""

    state: ExecutionNavigationState
    scope_error: str = ""
    credential_refresh_required: bool = False

    @property
    def write_count(self) -> int:
        """Expose the scalar write total supplied by navigation state."""
        return self.state.summary.write_count

    @property
    def current_run_id(self) -> str:
        """Expose the current durable run identifier for navigation controls."""
        return self.state.execution_run_id

    @property
    def current_run(self) -> str | None:
        """Expose the current run truthiness used by the review page."""

        return self.current_run_id or None

    @property
    def can_load(self) -> bool:
        """Return the navigation-level eligibility of the Load action.

        ``WorkspaceNavigationQueryService`` consumes this compact result. The
        full ``ExecutionPreview.can_load`` remains the command-time authority.
        """
        return bool(
            self.write_count
            and not self.current_run_id
            and not self.scope_error
            and not self.state.summary.has_attention
        )

    @property
    def can_complete_without_load(self) -> bool:
        """Offer the zero-write review without decoding execution rows."""
        return bool(
            not self.write_count
            and not self.current_run_id
            and not self.scope_error
            and not self.state.summary.has_attention
        )


def build_execution_preview(
    snapshot: ExecutionSnapshot,
    *,
    current_run: ExecutionRun | None,
    api_scope: OdooApiScope,
    deferred_create_count: int,
    scope_error: str = "",
    credential_refresh_required: bool = False,
    prior_interrupted_run: ExecutionRun | None = None,
    business_intent_assessment_hash: str = "",
    business_intent: BusinessIntentReview | None = None,
) -> ExecutionPreview:
    """Project captured facts without reading credentials or modifying evidence."""
    return ExecutionPreview(
        snapshot=snapshot,
        datasets=execution_dataset_previews(snapshot),
        current_run=current_run,
        api_scope=api_scope,
        deferred_create_count=deferred_create_count,
        scope_error=scope_error,
        scope_error_code=execution_scope_error_code(scope_error),
        credential_refresh_required=credential_refresh_required,
        dependency_summary=execution_dependency_summary(snapshot),
        blocker_summary=execution_blocker_summary(snapshot),
        prior_interrupted_run=prior_interrupted_run,
        business_intent_assessment_hash=business_intent_assessment_hash,
        business_intent=business_intent,
    )


def planned_deferred_create_count(
    snapshot: ExecutionSnapshot,
    *,
    create_batch_rows: int,
) -> int:
    """Count create rows expected to need the reviewed relationship pass."""
    validated_create_batch_rows(create_batch_rows)
    return len({item.row_id for item in snapshot.relationship_plan.completions})
