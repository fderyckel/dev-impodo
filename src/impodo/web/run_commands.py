"""Coordinate preparation commands and publish guarded run milestones."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from impodo.application.run.progress import (
    assert_application_is_current, current_preparation, next_unverified_application,
)
from impodo.application.workspace.execution.job_models import LoadJob, LoadJobStatus
from impodo.application.workspace.preparation.job_models import (
    PreparationJob, PreparationJobStatus, PreparationWorkspace,
)
from impodo.domain.project.foundation import MigrationConflictError, MigrationFoundationError
from impodo.domain.run.contracts import (
    MigrationRunPlanIssueLevel, RecipeApplicationStatus, RunRecipeApplication,
)
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.workspace.errors import WorkspaceError

if TYPE_CHECKING:
    from .context import WebContext


def enqueue_preparation(
    context: WebContext, workspace_id: str, *, retry_job_id: str | None = None,
) -> PreparationJob:
    """Resume current published work or start one mapping-bound attempt."""

    manager = context.preparation_jobs
    if manager is None:
        raise WorkspaceError("Background preparation is unavailable")
    if retry_job_id is not None:
        manager.get(workspace_id, retry_job_id)
    workspace = _preparation_workspace(context, workspace_id)
    if workspace.recipe_application_id is not None:
        restored = recover_run_preparation(context, workspace.migration_run_id)
        if restored is not None and restored.workspace_id == workspace_id:
            return restored
    workspace = _assert_recipe_application_can_prepare(context, workspace)
    total_rows = _preparation_row_count(context, workspace_id)
    if retry_job_id is not None:
        return manager.retry(
            workspace_id, retry_job_id, _migration_project_name(context, workspace.project_id),
            total_rows, actor=context.actor, workspace=workspace,
        )
    return manager.enqueue(
        workspace_id,
        _migration_project_name(context, workspace.project_id),
        total_rows,
        actor=context.actor,
        workspace=workspace,
    )


def _assert_recipe_application_can_prepare(
    context: WebContext,
    workspace: PreparationWorkspace,
) -> PreparationWorkspace:
    """Keep preparation behind the run's remaining Recipe reviews."""

    application_id = workspace.recipe_application_id
    if application_id is None:
        return workspace
    application = context.run_planning.repository.get_application(application_id)
    if application.workspace_id != workspace.workspace_id:
        raise WorkspaceError("This Recipe work area no longer matches its run")
    bundle = context.run_planning.repository.get_bundle(workspace.migration_run_id)
    if bundle.run.purpose is MigrationRunPurpose.PRODUCTION:
        operation = context.production_runs.activation_operation(workspace.migration_run_id, actor=context.actor)
        if operation is None or operation.state.value != "COMMITTED":
            raise WorkspaceError("Finish Production setup before preparing its work areas")
    assert_application_is_current(bundle, application)
    issues = context.run_planning.repository.list_issues(application_id)
    actionable = tuple(
        item for item in issues if item.level.value != "INFORMATION"
    )
    if application.status is RecipeApplicationStatus.BLOCKED or actionable:
        if actionable and all(
            item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
            for item in actionable
        ):
            raise WorkspaceError(
                "Review the current Odoo defaults before preparing this Recipe"
            )
        raise WorkspaceError(
            actionable[0].message
            if actionable
            else "Finish the remaining Recipe review before preparation"
        )
    if application.status not in {
        RecipeApplicationStatus.READY,
        RecipeApplicationStatus.RUNNING,
        RecipeApplicationStatus.FAILED,
    }:
        raise WorkspaceError(
            "Continue this Recipe from its current Review and load step"
        )
    return replace(workspace, mapping_content_hash=application.mapping_content_hash)


def _migration_project_name(context: WebContext, migration_project_id: str) -> str:
    """Return the business Project name without opening a workspace database."""

    return context.migration_projects.get(
        migration_project_id,
        actor=context.actor,
    ).display_name


def _preparation_workspace(
    context: WebContext,
    workspace_id: str,
) -> PreparationWorkspace:
    """Resolve one open workspace and its Project-owned DataVersion."""

    try:
        workspace = context.migration_workspaces.get(
            workspace_id,
            actor=context.actor,
        )
        data_version = context.data_versions.get(
            workspace.data_version_id,
            actor=context.actor,
        )
        run = context.migration_runs.get(
            workspace.migration_run_id,
            actor=context.actor,
        )
        prepared_workspace = PreparationWorkspace.from_context(
            workspace,
            data_version,
            run,
        )
        projection = (
            context.data_version_source_projection.projections.repository
            .get_workspace_source_projection(workspace_id)
        )
        if projection is None:
            return prepared_workspace
        return replace(
            prepared_workspace,
            source_package_hash=projection.package_hash,
            source_dataset_ids=tuple(
                item.dataset_id for item in projection.datasets
            ),
        )
    except MigrationFoundationError as error:
        raise WorkspaceError(
            f"{error}. No Odoo records were changed. Return to the Project "
            "overview and continue from its current authoring workspace."
        ) from error


def _preparation_row_count(context: WebContext, workspace_id: str) -> int:
    """Return optional progress metadata without bypassing worker validation."""

    try:
        selection = context.queries.get_source_selection(workspace_id)
    except WorkspaceError:
        # The worker owns authoritative validation and records a durable failed
        # job. Display metadata must not prevent that governed failure path.
        selection = None
    return sum(item.row_count for item in selection.datasets) if selection else 0


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
    next_application = next_unverified_application(bundle)
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
    latest = current_preparation(next_application, latest)
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
    return enqueue_preparation(context, next_application.workspace_id)


def recover_run_preparation(context: WebContext, migration_run_id: str) -> PreparationJob | None:
    """Inspect only the current Recipe when entering or resuming a run."""

    manager = context.preparation_jobs
    if manager is None:
        return None
    bundle = context.run_planning.repository.get_bundle(migration_run_id)
    application = next_unverified_application(bundle)
    if application is None or application.status not in {
        RecipeApplicationStatus.READY, RecipeApplicationStatus.RUNNING, RecipeApplicationStatus.FAILED,
    }:
        return None
    observed = manager.latest_many((application.workspace_id,)).get(application.workspace_id)
    latest = current_preparation(application, observed)
    if latest is not None:
        if latest.active:
            return latest
        if latest.status not in {
            PreparationJobStatus.SUCCEEDED, PreparationJobStatus.REVIEW_REQUIRED,
        } and latest.failure_code != "WORKER_EXITED":
            return None
    if context.load_jobs is not None and context.load_jobs.latest_many((application.workspace_id,)):
        return None
    if any(
        issue.level is not MigrationRunPlanIssueLevel.INFORMATION
        for issue in context.run_planning.repository.list_issues(application.application_id)
    ):
        return None
    result = context.preparation_recovery.current(
        application.workspace_id, application.mapping_content_hash, actor=context.actor,
    )
    if result is None:
        return None
    workspace = replace(
        _preparation_workspace(context, application.workspace_id),
        mapping_content_hash=application.mapping_content_hash,
    )
    restored = manager.restore_result(
        workspace, result, actor=context.actor,
        migration_project_name=_migration_project_name(context, application.project_id),
        expected_job_id=observed.job_id if observed else None,
    )
    if restored is not None:
        publish_preparation_progress(context, restored)
    return restored


def publish_preparation_progress(
    context: WebContext,
    job: PreparationJob,
) -> None:
    """Copy a worker milestone into the run registry."""

    application_id = job.workspace.recipe_application_id
    if application_id is None:
        return
    if getattr(job.workspace, "mapping_content_hash", None) is not None:
        application = context.run_planning.repository.get_application(application_id)
        if current_preparation(application, job) is None:
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
        expected_mapping_content_hash=getattr(job.workspace, "mapping_content_hash", None),
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
    expected_mapping_content_hash: str | None = None,
) -> RunRecipeApplication:
    """Publish one milestone without reversing held or later progress."""

    try:
        binding = ({"expected_mapping_content_hash": expected_mapping_content_hash}
                   if expected_mapping_content_hash is not None else {})
        return context.run_planning.repository.transition_application_status(
            application_id,
            expected_statuses=expected_statuses,
            status=status,
            actor=context.actor,
            **binding,
        )
    except MigrationConflictError:
        current = context.run_planning.repository.get_application(application_id)
        if expected_mapping_content_hash is not None and current.mapping_content_hash != expected_mapping_content_hash:
            return current
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


