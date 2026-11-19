"""Choose the project landing page action from the saved run and workspace evidence."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import TYPE_CHECKING

from impodo.application.correction_workflow import CorrectionJourneyView
from impodo.application.workspace.navigation import WorkspaceNavigationSnapshot
from impodo.application.workspace.views import WorkspaceOwnerView
from impodo.domain.data_version.models import DataVersion
from impodo.domain.project.models import MigrationProject, MigrationProjectStatus
from impodo.domain.run.models import (
    MigrationRun,
    MigrationRunPurpose,
    MigrationRunState,
)
from impodo.domain.run.production import ProductionRunBinding
from impodo.domain.run.test_setup import TestRunSetupBinding, TestRunSetupState
from impodo.domain.workspace.models import (
    MigrationWorkspace,
    MigrationWorkspaceSetupState,
    MigrationWorkspaceState,
)
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStateNotFoundError

from .project_workspace_action import (
    ProjectOverviewAction,
    closed_workspace_action,
    draft_workspace_action,
    workspace_action,
)

if TYPE_CHECKING:
    from ..context import WebContext


_FINISHED_RUN_STATES = {MigrationRunState.COMPLETED, MigrationRunState.CLOSED}


def project_overview_action(
    *,
    project: MigrationProject,
    authoring_workspace: MigrationWorkspace | None,
    authoring_data_version: DataVersion | None,
    runs: Sequence[MigrationRun],
    workspaces: Sequence[MigrationWorkspace],
    correction_journeys: Sequence[CorrectionJourneyView],
    test_bindings: Mapping[str, TestRunSetupBinding],
    production_bindings: Sequence[ProductionRunBinding],
    completed_production_setups: Collection[str],
    workspace_snapshot: Callable[[str], WorkspaceNavigationSnapshot],
    draft_source: Callable[[DataVersion], tuple[SourceMode, bool] | None] | None = None,
    active_jobs: WebContext | None = None,
    protected_results: bool = True,
) -> ProjectOverviewAction:
    """Read at most one workspace after choosing the relevant work from the registry."""

    if project.status in {
        MigrationProjectStatus.CLOSED,
        MigrationProjectStatus.ARCHIVED,
    }:
        status = project.status.value.capitalize()
        return ProjectOverviewAction("complete", status, f"Project {status.lower()}")

    recipe_runs = tuple(
        run for run in runs if run.purpose is not MigrationRunPurpose.AUTHORING
    )
    latest_run = max(
        (run for run in recipe_runs if run.state not in _FINISHED_RUN_STATES),
        key=lambda run: (run.updated_at, run.run_number),
        default=None,
    )
    if latest_run is not None and (
        authoring_workspace is None
        or latest_run.updated_at >= authoring_workspace.updated_at
    ):
        return _recipe_run_action(
            latest_run, test_bindings, production_bindings, completed_production_setups
        )

    if authoring_workspace is not None and authoring_data_version is not None:
        run = next(
            run
            for run in runs
            if run.migration_run_id == authoring_workspace.migration_run_id
        )
        owner = WorkspaceOwnerView(
            project, authoring_workspace, authoring_data_version, run, None
        )
        correction = next(
            (
                item
                for item in correction_journeys
                if item.successor_workspace_id == owner.workspace_id
            ),
            None,
        )
        if (
            draft_source
            and authoring_workspace.setup_state is MigrationWorkspaceSetupState.DRAFT
            and correction is None
        ):
            source = draft_source(authoring_data_version)
            if source is None:
                return ProjectOverviewAction(
                    "current", "Setup", "Project setup is incomplete"
                )
            return draft_workspace_action(owner.workspace_id, *source)
        try:
            snapshot = workspace_snapshot(owner.workspace_id)
        except WorkspaceStateNotFoundError:
            return ProjectOverviewAction(
                "attention",
                "Needs attention",
                "Workspace unavailable",
                authoring_workspace.display_name,
            )
        return workspace_action(
            owner,
            snapshot,
            correction,
            active_jobs,
            protected_results=protected_results,
        )

    authoring_run_ids = {
        run.migration_run_id
        for run in runs
        if run.purpose is MigrationRunPurpose.AUTHORING
    }
    closed = max(
        (
            workspace
            for workspace in workspaces
            if workspace.state is MigrationWorkspaceState.CLOSED
            and workspace.migration_run_id in authoring_run_ids
        ),
        key=lambda item: (item.closed_at or item.updated_at, item.workspace_id),
        default=None,
    )
    finished = max(
        recipe_runs, key=lambda run: (run.updated_at, run.run_number), default=None
    )
    if finished is not None and (
        closed is None or finished.updated_at > (closed.closed_at or closed.updated_at)
    ):
        return _recipe_run_action(
            finished, test_bindings, production_bindings, completed_production_setups
        )
    if closed is not None:
        try:
            snapshot = workspace_snapshot(closed.workspace_id)
        except WorkspaceStateNotFoundError:
            return ProjectOverviewAction(
                "attention",
                "Needs attention",
                "Saved workspace unavailable",
                closed.display_name,
            )
        return closed_workspace_action(
            closed, snapshot, correction_journeys, protected_results=protected_results
        )
    return ProjectOverviewAction(
        "current", "Setup", "No active work in this project", saved_data_candidate=True
    )


def _recipe_run_action(
    run: MigrationRun,
    test_bindings: Mapping[str, TestRunSetupBinding],
    production_bindings: Sequence[ProductionRunBinding],
    completed_production_setups: Collection[str],
) -> ProjectOverviewAction:
    kind = run.purpose.value.title()
    run_id = run.migration_run_id
    base = f"/projects/{run.project_id}"
    home = f"{base}/runs/{run_id}"
    if run.state in _FINISHED_RUN_STATES:
        status = run.state.value.capitalize()
        return ProjectOverviewAction(
            "complete",
            status,
            f"{kind} run {status.lower()}",
            run.label,
            "View run",
            home,
        )
    test = test_bindings.get(run_id)
    if test and test.state is TestRunSetupState.SETUP:
        return ProjectOverviewAction(
            "current",
            "Setup",
            "Test setup",
            run.label,
            "Continue Test setup",
            f"{base}/test-runs/{run_id}/fresh-data",
        )
    if (
        any(item.migration_run_id == run_id for item in production_bindings)
        and run_id not in completed_production_setups
    ):
        return ProjectOverviewAction(
            "current",
            "Setup",
            "Production setup",
            run.label,
            "Continue Production setup",
            f"{base}/production-runs/{run_id}/activate",
        )
    needs_attention = run.state is MigrationRunState.INCOMPLETE
    return ProjectOverviewAction(
        "attention" if needs_attention else "current",
        "Needs attention" if needs_attention else "In progress",
        f"{kind} run",
        run.label,
        f"Open {kind} run",
        home,
    )
