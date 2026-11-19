"""Present the selected workspace's next stage, live progress or recorded load result."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from impodo.application.correction_workflow import CorrectionJourneyView
from impodo.application.workspace.execution.navigation import ExecutionNavigationState
from impodo.application.workspace.navigation import WorkspaceNavigationSnapshot
from impodo.application.workspace.views import WorkspaceOwnerView
from impodo.domain.reconciliation import ReconciliationRunStatus
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStatus

from .navigation import WorkflowStage, build_workspace_navigation

if TYPE_CHECKING:
    from ..context import WebContext


@dataclass(frozen=True, slots=True)
class ProjectOverviewAction:
    """One destination that reflects the current work or its saved result."""

    status: str
    status_label: str
    title: str
    detail: str = ""
    action_label: str | None = None
    href: str | None = None
    source_mode: SourceMode | None = None
    workbook_label: str | None = None
    workbook_href: str | None = None
    saved_data_candidate: bool = False
    featured_saved_set_id: str | None = None


def closed_workspace_action(
    workspace: MigrationWorkspace,
    snapshot: WorkspaceNavigationSnapshot,
    journeys: Sequence[CorrectionJourneyView],
    *,
    protected_results: bool,
) -> ProjectOverviewAction:
    """Open the retained result when verification evidence exists."""

    if outcome := _load_outcome(snapshot):
        return _visible_result(outcome, protected_results)
    verified = any(
        item.completed_workspace_id == workspace.workspace_id
        or (item.completed and item.successor_workspace_id == workspace.workspace_id)
        for item in journeys
    )
    result = ProjectOverviewAction(
        "complete",
        "Verified" if verified else "Closed",
        "Load verified" if verified else "Saved workspace",
        workspace.display_name,
        "View load results" if verified else "View saved stages",
        f"/workspaces/{workspace.workspace_id}/history/6",
        snapshot.workspace_state.source_mode,
    )
    return _visible_result(result, protected_results)


def workspace_action(
    owner: WorkspaceOwnerView,
    snapshot: WorkspaceNavigationSnapshot,
    correction: CorrectionJourneyView | None,
    active_jobs: WebContext | None,
    *,
    protected_results: bool,
) -> ProjectOverviewAction:
    """Continue at the live job, saved outcome, or current stage for this workspace."""

    workspace = snapshot.workspace_state
    facts = snapshot.facts
    navigation = build_workspace_navigation(
        facts,
        workspace,
        "workspace_overview.html",
        workspace_view=owner,
        continuation=correction is not None,
        continuation_plan_ready=bool(correction and correction.has_current_plan),
    )
    mode = workspace.source_mode
    if workspace.status is WorkspaceStatus.DRAFT:
        return draft_workspace_action(
            owner.workspace_id, mode, bool(workspace.source_files)
        )
    if progress := _progress_action(snapshot, correction, active_jobs):
        return progress
    if outcome := _load_outcome(snapshot):
        return _visible_result(outcome, protected_results)

    stage = navigation.current_stage
    if stage is None or stage.href is None:
        return ProjectOverviewAction(
            "current",
            "In progress",
            "Workspace in progress",
            "",
            "Open workspace",
            f"/workspaces/{owner.workspace_id}/overview",
            mode,
        )
    detail = f"Stage {stage.number} of {len(navigation.stages)}"
    if correction:
        detail += " · Working revision"
    action = _stage_action(stage, detail, mode)
    if (
        mode is SourceMode.ODOO
        and stage.stage_id == "select"
        and not (
            workspace.destination_odoo_connection_mode
            or workspace.saved_restore_generation_id
            or facts.transfer_execution_run_id
        )
    ):
        return replace(action, saved_data_candidate=True)
    return action


def draft_workspace_action(
    workspace_id: str, mode: SourceMode, has_files: bool
) -> ProjectOverviewAction:
    """Show the source-mode setup before any workspace navigation is necessary."""

    if mode is SourceMode.ODOO:
        return ProjectOverviewAction(
            "current",
            "Setup",
            "Set up your Odoo transfer",
            "",
            "Connect source Odoo",
            f"/workspaces/{workspace_id}/target",
            mode,
            saved_data_candidate=True,
        )
    return ProjectOverviewAction(
        "current",
        "Setup",
        "Set up your file import",
        "",
        "Review source files" if has_files else "Add source files",
        f"/workspaces/{workspace_id}/files",
        mode,
        saved_data_candidate=not has_files,
    )


def _progress_action(
    snapshot: WorkspaceNavigationSnapshot,
    correction: CorrectionJourneyView | None,
    jobs: WebContext | None,
) -> ProjectOverviewAction | None:
    facts = snapshot.facts
    workspace = snapshot.workspace_state
    mode = workspace.source_mode
    base = f"/workspaces/{workspace.workspace_id}"
    if facts.active_load_job_id:
        area = "transfer-load" if mode is SourceMode.ODOO else "load"
        title = "Loading into Odoo"
        href = f"{base}/{area}/progress/{facts.active_load_job_id}"
    elif facts.active_preparation_job_id:
        title = "Preparing data"
        href = f"{base}/preparation/{facts.active_preparation_job_id}"
    elif jobs and correction:
        job = jobs.correction_jobs.active(correction.completed_workspace_id)
        if job is None or job.successor_workspace_id != workspace.workspace_id:
            return None
        title = (
            "Checking the working revision"
            if job.kind.value == "REVIEW"
            else "Loading reviewed changes"
        )
        href = f"/workspaces/{correction.completed_workspace_id}/correction/progress/{job.job_id}?continue=1"
    elif jobs and mode is SourceMode.ODOO and jobs.odoo_capture_jobs:
        capture = jobs.odoo_capture_jobs.active(workspace.workspace_id)
        if capture is None:
            return None
        title = "Downloading source data"
        href = f"{base}/sources/odoo-capture/{capture.job_id}"
    elif (
        jobs
        and mode is SourceMode.FILE
        and jobs.preflight_jobs
        and facts.normalization_frozen
    ):
        comparison = jobs.preflight_jobs.active(
            workspace.workspace_id, session_only=True
        )
        if comparison is None:
            return None
        title = "Comparing with Odoo"
        href = f"{base}/preflight/{comparison.job_id}"
    else:
        return None
    return ProjectOverviewAction(
        "current", "In progress", title, "", "View progress", href, mode
    )


def _stage_action(
    stage: WorkflowStage, detail: str, mode: SourceMode
) -> ProjectOverviewAction:
    attention = stage.status == "attention"
    page = next(
        (page for page in stage.pages if page.status == "attention" and page.href), None
    )
    if attention and page:
        return ProjectOverviewAction(
            "attention",
            "Needs attention",
            stage.label,
            detail,
            page.label,
            page.href,
            mode,
        )
    return ProjectOverviewAction(
        "attention" if attention else "current",
        "Needs attention" if attention else "In progress",
        stage.label,
        detail,
        "Review findings" if attention else f"Continue to {stage.label}",
        stage.href,
        mode,
    )


def _load_outcome(
    snapshot: WorkspaceNavigationSnapshot,
) -> ProjectOverviewAction | None:
    workspace = snapshot.workspace_state
    facts = snapshot.facts
    if workspace.source_mode is SourceMode.ODOO:
        return _transfer_outcome(snapshot) if facts.transfer_execution_run_id else None
    state = facts.execution_state
    if state is None or not state.execution_run_id:
        return None
    if state.reconciliation_status == ReconciliationRunStatus.VERIFIED:
        return _verified_load(snapshot, state)
    reconciled = bool(state.reconciliation_status)
    base = f"/workspaces/{workspace.workspace_id}/load"
    workbook = _workbook_href(base, state)
    return ProjectOverviewAction(
        "attention",
        "Needs attention",
        "Load needs attention" if reconciled else "Check the load outcome",
        _outcome_detail(snapshot),
        "Review load results" if reconciled else "Check load outcome",
        f"{base}/outcome",
        workspace.source_mode,
        "Download highlighted source workbook" if workbook else None,
        workbook,
    )


def _visible_result(
    result: ProjectOverviewAction, protected_results: bool
) -> ProjectOverviewAction:
    if protected_results:
        return result
    return replace(
        result,
        detail="",
        action_label=None,
        href=None,
        workbook_href=None,
        workbook_label=None,
    )


def _workbook_href(base: str, state: ExecutionNavigationState) -> str | None:
    if state.has_workbook_detail and state.reconciliation_id:
        return f"{base}/fallout.xlsx?verification={state.reconciliation_id}"
    return None


def _verified_load(
    snapshot: WorkspaceNavigationSnapshot, state: ExecutionNavigationState
) -> ProjectOverviewAction:
    workspace = snapshot.workspace_state
    summary = state.summary
    counts = f"{summary.create_count:,} created · {summary.update_count:,} updated · {summary.unchanged_count:,} unchanged"
    outcome = snapshot.facts.project_load_outcome
    destination = state.target_database
    if outcome:
        destination = " · ".join(filter(None, (outcome.database, outcome.company_name)))
    elif (
        workspace.odoo_company
        and workspace.odoo_company.target_hash == summary.target_hash
    ):
        destination = " · ".join(
            filter(None, (destination, workspace.odoo_company.name))
        )
    details = (
        destination,
        state.verified_at.strftime("%d %b %Y") if state.verified_at else "",
        counts,
    )
    base = f"/workspaces/{workspace.workspace_id}/load"
    workbook = _workbook_href(base, state)
    return ProjectOverviewAction(
        "complete",
        "Verified",
        "Load verified" if summary.write_count else "No changes needed",
        " · ".join(filter(None, details)),
        "View load results",
        f"{base}/outcome",
        workspace.source_mode,
        "Download verified source workbook" if workbook else None,
        workbook,
    )


def _transfer_outcome(snapshot: WorkspaceNavigationSnapshot) -> ProjectOverviewAction:
    workspace = snapshot.workspace_state
    verified = (
        snapshot.facts.transfer_reconciliation_status
        == ReconciliationRunStatus.VERIFIED
    )
    company = workspace.destination_company
    return ProjectOverviewAction(
        "complete" if verified else "attention",
        "Verified" if verified else "Needs attention",
        "Transfer verified" if verified else "Check the transfer outcome",
        _outcome_detail(snapshot)
        if snapshot.facts.project_load_outcome
        else " · ".join(
            filter(
                None,
                (workspace.destination_odoo_database, company.name if company else ""),
            )
        ),
        "View transfer results",
        f"/workspaces/{workspace.workspace_id}/transfer-load/outcome",
        workspace.source_mode,
    )


def _outcome_detail(snapshot: WorkspaceNavigationSnapshot) -> str:
    outcome = snapshot.facts.project_load_outcome
    if outcome is None:
        return ""
    return " · ".join(
        filter(
            None,
            (
                outcome.database,
                outcome.company_name,
                outcome.verified_at.strftime("%d %b %Y") if outcome.verified_at else "",
                outcome.detail,
            ),
        )
    )
