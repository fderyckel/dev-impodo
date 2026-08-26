"""Project bounded Recipe-run progress into one Review and load page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from ..domain.serialization import content_hash
from ..load_jobs import LoadJob, LoadJobStatus
from ..migration_foundation import MigrationConflictError
from ..migration_run_planning import (
    IntegratedRunBundle,
    MigrationRunPlanIssue,
    RecipeApplicationStatus,
    RunRecipeApplication,
)
from ..migration_runs import MigrationRunPurpose
from ..preparation_jobs import PreparationJob, PreparationJobStatus
from ..workspace_errors import WorkspaceError

if TYPE_CHECKING:
    from .context import WebContext


@dataclass(frozen=True, slots=True)
class RunApplicationCard:
    """Show one ordered Recipe without reading its workspace database."""

    application: RunRecipeApplication
    recipe_name: str
    state: str
    state_label: str
    message: str
    status_class: str
    action_label: str
    action_url: str
    action_method: str
    progress_percent: int | None
    progress_message: str
    issues: tuple[MigrationRunPlanIssue, ...]
    current: bool


@dataclass(frozen=True, slots=True)
class IntegratedRunReviewView:
    """One bounded run summary plus session-owned worker snapshots."""

    cards: tuple[RunApplicationCard, ...]
    completed_count: int
    total_count: int
    active: bool
    view_hash: str


def build_integrated_run_review(
    context: WebContext,
    bundle: IntegratedRunBundle,
    *,
    recipes: Mapping[str, object],
    issues: Mapping[str, tuple[MigrationRunPlanIssue, ...]],
) -> IntegratedRunReviewView:
    """Build the final-page projection without opening application stores."""

    by_recipe = {item.recipe_id: item for item in bundle.applications}
    ordered = tuple(
        by_recipe[recipe_id]
        for recipe_id in bundle.requirement_plan.application_order
    )
    workspace_ids = tuple(item.workspace_id for item in ordered)
    preparations = (
        context.preparation_jobs.latest_many(workspace_ids)
        if context.preparation_jobs is not None
        else {}
    )
    loads = (
        context.load_jobs.latest_many(workspace_ids)
        if context.load_jobs is not None
        else {}
    )
    complete_ids = {
        item.application_id
        for item in ordered
        if _application_is_complete(item, loads.get(item.workspace_id))
    }
    current_id = next(
        (
            item.application_id
            for item in ordered
            if item.application_id not in complete_ids
        ),
        None,
    )
    cards = tuple(
        _application_card(
            item,
            recipe_name=str(
                getattr(recipes.get(item.recipe_id), "display_name", "Recipe")
            ),
            preparation=preparations.get(item.workspace_id),
            load=loads.get(item.workspace_id),
            issues=issues.get(item.application_id, ()),
            current=item.application_id == current_id,
            automatic_preparation=bundle.run.purpose is MigrationRunPurpose.TEST,
        )
        for item in ordered
    )
    view_hash = content_hash(
        [
            {
                "application_id": card.application.application_id,
                "application_status": card.application.status.value,
                "state": card.state,
                "progress_percent": card.progress_percent,
                "progress_message": card.progress_message,
            }
            for card in cards
        ]
    )
    return IntegratedRunReviewView(
        cards=cards,
        completed_count=len(complete_ids),
        total_count=len(cards),
        active=any(card.state in {"PREPARING", "LOADING"} for card in cards),
        view_hash=view_hash,
    )


def start_next_preparation(
    context: WebContext,
    migration_run_id: str,
) -> PreparationJob:
    """Start only the first unresolved Test application in saved order."""

    if context.preparation_jobs is None:
        raise WorkspaceError("Background preparation is unavailable")
    bundle = context.run_planning.repository.get_bundle(migration_run_id)
    if bundle.run.purpose is not MigrationRunPurpose.TEST:
        raise WorkspaceError(
            "Automatic preparation is currently available for Test runs only"
        )
    by_recipe = {item.recipe_id: item for item in bundle.applications}
    ordered = tuple(
        by_recipe[recipe_id]
        for recipe_id in bundle.requirement_plan.application_order
    )
    next_application = next(
        (
            item
            for item in ordered
            if item.status
            not in {
                RecipeApplicationStatus.RECONCILED,
                RecipeApplicationStatus.QUALIFIED,
            }
        ),
        None,
    )
    if next_application is None:
        raise WorkspaceError("Every Recipe in this Test run is already verified")
    if next_application.status is RecipeApplicationStatus.BLOCKED:
        issue = context.run_planning.repository.list_issues(
            next_application.application_id
        )
        detail = issue[0].message if issue else "This Recipe needs attention"
        raise WorkspaceError(detail)
    if next_application.status not in {
        RecipeApplicationStatus.READY,
        RecipeApplicationStatus.RUNNING,
        RecipeApplicationStatus.FAILED,
    }:
        raise WorkspaceError(
            "Finish the current Recipe review and load before preparing the next one"
        )
    latest = context.preparation_jobs.latest_many(
        (next_application.workspace_id,)
    ).get(next_application.workspace_id)
    if latest is not None:
        if latest.active:
            return latest
        if latest.status in {
            PreparationJobStatus.SUCCEEDED,
            PreparationJobStatus.REVIEW_REQUIRED,
        }:
            raise WorkspaceError(
                "Prepared data is waiting for review in this Recipe"
            )
        if not latest.retry_allowed:
            raise WorkspaceError(
                "Restart Impodo before preparing this saved work again"
            )
    # Import locally so the preparation router remains the one command owner.
    from .routers.preparation import enqueue_preparation

    return enqueue_preparation(context, next_application.workspace_id)


def publish_preparation_progress(
    context: WebContext,
    job: PreparationJob,
) -> None:
    """Copy a worker milestone into the run registry."""

    application_id = job.workspace.recipe_application_id
    if application_id is None:
        return
    if job.status is PreparationJobStatus.RUNNING:
        target = RecipeApplicationStatus.RUNNING
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.FAILED,
        )
    elif job.status is PreparationJobStatus.SUCCEEDED:
        target = RecipeApplicationStatus.PREPARED
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
            RecipeApplicationStatus.FAILED,
        )
    elif job.status is PreparationJobStatus.REVIEW_REQUIRED:
        target = RecipeApplicationStatus.RUNNING
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.FAILED,
        )
    elif job.status in {
        PreparationJobStatus.FAILED,
        PreparationJobStatus.CANCELLED,
    }:
        target = RecipeApplicationStatus.FAILED
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
        )
    else:
        return
    _transition_application_progress(
        context,
        application_id,
        expected_statuses=expected,
        status=target,
    )


def publish_load_progress(context: WebContext, job: LoadJob) -> None:
    """Copy a confirmed-load milestone into the run registry."""

    application_id = job.access_context.recipe_application_id
    if application_id is None:
        return
    if job.status is LoadJobStatus.RUNNING:
        target = RecipeApplicationStatus.COMPARED
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
            RecipeApplicationStatus.PREPARED,
        )
    elif job.status is LoadJobStatus.SUCCEEDED:
        target = (
            RecipeApplicationStatus.RECONCILED
            if job.verification_complete
            else RecipeApplicationStatus.EXECUTED
        )
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
            RecipeApplicationStatus.PREPARED,
            RecipeApplicationStatus.COMPARED,
            RecipeApplicationStatus.EXECUTED,
        )
    elif job.status is LoadJobStatus.FAILED:
        target = RecipeApplicationStatus.COMPARED
        expected = (
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
            RecipeApplicationStatus.PREPARED,
            RecipeApplicationStatus.COMPARED,
        )
    else:
        return
    updated = _transition_application_progress(
        context,
        application_id,
        expected_statuses=expected,
        status=target,
    )
    if updated.status is RecipeApplicationStatus.RECONCILED:
        _try_start_after_reconciliation(context, updated.migration_run_id)


_APPLICATION_PROGRESS_ORDER = {
    RecipeApplicationStatus.READY: 0,
    RecipeApplicationStatus.RUNNING: 1,
    RecipeApplicationStatus.PREPARED: 2,
    RecipeApplicationStatus.COMPARED: 3,
    RecipeApplicationStatus.EXECUTED: 4,
    RecipeApplicationStatus.RECONCILED: 5,
    RecipeApplicationStatus.QUALIFIED: 5,
}


def _transition_application_progress(
    context: WebContext,
    application_id: str,
    *,
    expected_statuses: tuple[RecipeApplicationStatus, ...],
    status: RecipeApplicationStatus,
) -> RunRecipeApplication:
    """Publish one milestone without reversing held or later progress."""

    try:
        return context.run_planning.repository.transition_application_status(
            application_id,
            expected_statuses=expected_statuses,
            status=status,
            actor=context.actor,
        )
    except MigrationConflictError:
        current = context.run_planning.repository.get_application(application_id)
        current_order = _APPLICATION_PROGRESS_ORDER.get(current.status)
        target_order = _APPLICATION_PROGRESS_ORDER.get(status)
        if current.status is RecipeApplicationStatus.BLOCKED or (
            current_order is not None
            and target_order is not None
            and current_order >= target_order
        ):
            return current
        raise


def publish_compared_application(
    context: WebContext,
    application_id: str,
    migration_run_id: str,
) -> None:
    """Record that current prepared data was checked against the run target."""

    current = context.run_planning.repository.get_application(application_id)
    if current.migration_run_id != migration_run_id:
        raise WorkspaceError(
            "The compared Recipe does not belong to this run"
        )
    context.run_planning.repository.transition_application_status(
        application_id,
        expected_statuses=(
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
            RecipeApplicationStatus.PREPARED,
            RecipeApplicationStatus.COMPARED,
        ),
        status=RecipeApplicationStatus.COMPARED,
        actor=context.actor,
    )


def publish_reconciled_application(
    context: WebContext,
    application_id: str,
    migration_run_id: str,
) -> None:
    """Unlock the next Recipe only after a clean verified read-back."""

    current = context.run_planning.repository.get_application(application_id)
    if current.migration_run_id != migration_run_id:
        raise WorkspaceError(
            "The verified Recipe does not belong to this Test run"
        )
    context.run_planning.repository.transition_application_status(
        application_id,
        expected_statuses=(
            RecipeApplicationStatus.READY,
            RecipeApplicationStatus.RUNNING,
            RecipeApplicationStatus.PREPARED,
            RecipeApplicationStatus.COMPARED,
            RecipeApplicationStatus.EXECUTED,
        ),
        status=RecipeApplicationStatus.RECONCILED,
        actor=context.actor,
    )
    _try_start_after_reconciliation(context, migration_run_id)


def _try_start_after_reconciliation(
    context: WebContext,
    migration_run_id: str,
) -> None:
    try:
        start_next_preparation(context, migration_run_id)
    except WorkspaceError:
        # The run page retains the exact next action. A completed run or a
        # current-data decision is an expected reason not to enqueue.
        return


def _application_is_complete(
    application: RunRecipeApplication,
    load: LoadJob | None,
) -> bool:
    return application.status in {
        RecipeApplicationStatus.RECONCILED,
        RecipeApplicationStatus.QUALIFIED,
    } or bool(
        load is not None
        and load.status is LoadJobStatus.SUCCEEDED
        and load.verification_complete
    )


def _application_card(
    application: RunRecipeApplication,
    *,
    recipe_name: str,
    preparation: PreparationJob | None,
    load: LoadJob | None,
    issues: tuple[MigrationRunPlanIssue, ...],
    current: bool,
    automatic_preparation: bool,
) -> RunApplicationCard:
    base_url = (
        f"/projects/{application.project_id}/runs/"
        f"{application.migration_run_id}/applications/{application.application_id}"
    )
    if _application_is_complete(application, load):
        return RunApplicationCard(
            application,
            recipe_name,
            "VERIFIED",
            "Verified",
            "Odoo accepted the load and Impodo verified the result.",
            "registered",
            "Review verified result",
            base_url,
            "get",
            100,
            "Verification complete",
            issues,
            current,
        )
    if not current:
        return RunApplicationCard(
            application,
            recipe_name,
            "WAITING",
            "Waiting",
            "Impodo will start this Recipe after the previous one is verified.",
            "draft",
            "",
            "",
            "",
            None,
            "",
            issues,
            current,
        )
    if load is not None and load.active:
        return RunApplicationCard(
            application,
            recipe_name,
            "LOADING",
            "Loading and verifying",
            load.message,
            "review",
            "Follow load",
            base_url,
            "get",
            load.progress_percent,
            load.message,
            issues,
            current,
        )
    if load is not None and load.status is LoadJobStatus.FAILED:
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Action needed",
            load.failure_message or "The Odoo load stopped before verification.",
            "blocked",
            "Review load",
            base_url,
            "get",
            load.progress_percent,
            load.message,
            issues,
            current,
        )
    if load is not None and load.status is LoadJobStatus.SUCCEEDED:
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Verify result",
            "The load finished, but its Odoo result still needs verification.",
            "review",
            "Verify result",
            base_url,
            "get",
            100,
            load.message,
            issues,
            current,
        )
    if preparation is not None and preparation.active:
        return RunApplicationCard(
            application,
            recipe_name,
            "PREPARING",
            "Preparing fresh data",
            preparation.message,
            "review",
            "Follow preparation",
            base_url,
            "get",
            preparation.progress_percent,
            preparation.message,
            issues,
            current,
        )
    if preparation is not None and preparation.status in {
        PreparationJobStatus.FAILED,
        PreparationJobStatus.CANCELLED,
    }:
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Action needed",
            preparation.failure_message or "Preparation stopped safely.",
            "blocked" if preparation.status is PreparationJobStatus.FAILED else "review",
            "Try preparation again" if preparation.retry_allowed else "Review preparation",
            base_url if not preparation.retry_allowed else (
                f"/projects/{application.project_id}/runs/"
                f"{application.migration_run_id}/prepare-next"
            ),
            "get" if not preparation.retry_allowed else "post",
            preparation.progress_percent,
            preparation.message,
            issues,
            current,
        )
    if preparation is not None and preparation.status is PreparationJobStatus.REVIEW_REQUIRED:
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Action needed",
            "Possible duplicate records need your review.",
            "review",
            "Review possible duplicates",
            base_url,
            "get",
            100,
            preparation.message,
            issues,
            current,
        )
    actionable_issues = tuple(
        item for item in issues if item.level.value != "INFORMATION"
    )
    default_reviews = tuple(
        item
        for item in actionable_issues
        if item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
        and item.level.value == "REVIEW"
    )
    if default_reviews and len(default_reviews) == len(actionable_issues):
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Review Odoo defaults",
            (
                f"Odoo can provide {len(default_reviews)} required value"
                f"{'s' if len(default_reviews) != 1 else ''} for this run."
            ),
            "review",
            "Review Odoo defaults",
            f"{base_url}/odoo-defaults",
            "get",
            None,
            "",
            issues,
            current,
        )
    if actionable_issues and all(
        item.code == "RECIPE_TARGET_NEW_REQUIRED_FIELD"
        for item in actionable_issues
    ):
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Check Odoo defaults",
            (
                "Odoo added required fields after this Recipe was published. "
                "Check whether this target supplies safe create defaults."
            ),
            "blocked",
            "Check Odoo defaults",
            (
                f"/projects/{application.project_id}/runs/"
                f"{application.migration_run_id}/odoo"
            ),
            "get",
            None,
            "",
            issues,
            current,
        )
    interrupted_default_recovery = (
        application.mapping_id is None
        and bool(default_reviews)
        and any(
            item.code == "RECIPE_MAPPING_MATERIALIZATION_BLOCKED"
            for item in actionable_issues
        )
        and all(
            item in default_reviews
            or item.code == "RECIPE_MAPPING_MATERIALIZATION_BLOCKED"
            for item in actionable_issues
        )
    )
    if interrupted_default_recovery:
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Finish checking Odoo defaults",
            (
                "Odoo supplied the required values, but Impodo did not finish "
                "creating this Recipe work area. Retry the saved check."
            ),
            "blocked",
            "Retry Odoo defaults",
            (
                f"/projects/{application.project_id}/runs/"
                f"{application.migration_run_id}/odoo"
            ),
            "get",
            None,
            "",
            issues,
            current,
        )
    if application.status is RecipeApplicationStatus.BLOCKED or actionable_issues:
        first = actionable_issues[0] if actionable_issues else None
        has_mapping_blocker = application.mapping_id is not None and any(
            item.code.startswith("MAPPING_")
            for item in actionable_issues
        )
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Review field matches" if has_mapping_blocker else "Action needed",
            first.message if first is not None else "This Recipe needs review.",
            "blocked",
            "Review field matches" if has_mapping_blocker else "",
            (
                f"/workspaces/{application.workspace_id}/mapping"
                if has_mapping_blocker
                else ""
            ),
            "get" if has_mapping_blocker else "",
            None,
            "",
            issues,
            current,
        )
    if application.status is RecipeApplicationStatus.EXECUTED:
        return RunApplicationCard(
            application,
            recipe_name,
            "ACTION_NEEDED",
            "Verify result",
            "The Odoo result still needs verification.",
            "review",
            "Verify result",
            base_url,
            "get",
            100,
            "Load recorded",
            issues,
            current,
        )
    if application.status is RecipeApplicationStatus.COMPARED:
        return RunApplicationCard(
            application,
            recipe_name,
            "READY_FOR_REVIEW",
            "Changes checked",
            "Review the proposed Odoo changes and confirm the load when ready.",
            "ready",
            "Review changes and load",
            base_url,
            "get",
            100,
            "Comparison complete",
            issues,
            current,
        )
    if application.status is RecipeApplicationStatus.PREPARED or (
        preparation is not None
        and preparation.status is PreparationJobStatus.SUCCEEDED
    ):
        return RunApplicationCard(
            application,
            recipe_name,
            "READY_FOR_REVIEW",
            "Ready for review",
            "Review prepared rows, exclusions, warnings, and the proposed load.",
            "ready",
            "Review prepared data",
            base_url,
            "get",
            100,
            "Preparation complete",
            issues,
            current,
        )
    return RunApplicationCard(
        application,
        recipe_name,
        "READY_TO_PREPARE",
        "Ready to prepare",
        (
            "Impodo can apply the saved Recipe to this fresh data now."
            if automatic_preparation
            else "Continue with this Recipe when the Production checks are complete."
        ),
        "ready",
        "Start preparation" if automatic_preparation else "Continue review and load",
        (
            f"/projects/{application.project_id}/runs/"
            f"{application.migration_run_id}/prepare-next"
            if automatic_preparation
            else base_url
        ),
        "post" if automatic_preparation else "get",
        0,
        "Waiting to start",
        issues,
        current,
    )
