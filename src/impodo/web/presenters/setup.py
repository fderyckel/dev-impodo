"""Present draft setup readiness without duplicating lifecycle rules."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceSetupRequirement,
    WorkspaceSetupStep,
    SourceMode,
    workspace_setup_requirements,
)


@dataclass(frozen=True, slots=True)
class WorkspaceSetupStepView:
    """Render one compact step in the draft setup navigation."""

    step_id: str
    label: str
    href: str | None
    current: bool
    status: str
    status_label: str
    requirements: tuple[WorkspaceSetupRequirement, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceSetupRecoveryView:
    """Group exceptional confirmation blockers by their recovery page."""

    step_id: str
    label: str
    href: str
    requirements: tuple[WorkspaceSetupRequirement, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceSetupView:
    """Provide the setup navigation and the current page's useful blockers."""

    steps: tuple[WorkspaceSetupStepView, ...]
    current_requirements: tuple[WorkspaceSetupRequirement, ...]
    recovery_steps: tuple[WorkspaceSetupRecoveryView, ...]


_STEP_LABELS = {
    WorkspaceSetupStep.FILES: "Source files",
    WorkspaceSetupStep.TARGET: "Connect Odoo",
}

_TEMPLATE_STEPS = {
    "workspace_files.html": WorkspaceSetupStep.FILES,
    "workspace_target.html": WorkspaceSetupStep.TARGET,
    "workspace_review.html": WorkspaceSetupStep.REVIEW,
}


def workspace_setup_step_order(
    workspace_state: WorkspaceState,
) -> tuple[WorkspaceSetupStep, ...]:
    """Return the setup sequence for the draft's selected source mode."""

    return (
        (WorkspaceSetupStep.FILES,)
        if workspace_state.source_mode is SourceMode.FILE
        else (WorkspaceSetupStep.TARGET,)
    )


def build_workspace_setup_view(
    workspace_state: WorkspaceState,
    template_name: str,
) -> WorkspaceSetupView:
    """Build one request-scoped setup view from the current project only."""

    requirements = workspace_setup_requirements(workspace_state)
    current_step = _TEMPLATE_STEPS.get(template_name)
    order = workspace_setup_step_order(workspace_state)
    step_views: list[WorkspaceSetupStepView] = []
    recovery_views: list[WorkspaceSetupRecoveryView] = []
    earlier_steps_complete = True

    for step in order:
        step_requirements = tuple(
            item for item in requirements if item.step is step
        )
        current = step is current_step
        if not earlier_steps_complete:
            status = "locked"
            status_label = "Complete an earlier step first"
            href = None
        elif current:
            status = "attention" if step_requirements else "current"
            status_label = (
                "Needs attention" if step_requirements else "Current"
            )
            href = _setup_step_url(workspace_state.workspace_id, step)
        elif step_requirements:
            status = "attention"
            status_label = "Needs attention"
            href = _setup_step_url(workspace_state.workspace_id, step)
        else:
            status = "complete"
            status_label = "Complete"
            href = _setup_step_url(workspace_state.workspace_id, step)

        step_views.append(
            WorkspaceSetupStepView(
                step_id=step.value,
                label=_STEP_LABELS[step],
                href=href,
                current=current,
                status=status,
                status_label=status_label,
                requirements=step_requirements,
            )
        )
        if step_requirements:
            recovery_views.append(
                WorkspaceSetupRecoveryView(
                    step_id=step.value,
                    label=_STEP_LABELS[step],
                    href=_setup_step_url(workspace_state.workspace_id, step),
                    requirements=step_requirements,
                )
            )
            earlier_steps_complete = False

    current_requirements = tuple(
        item for item in requirements if item.step is current_step
    )
    return WorkspaceSetupView(
        steps=tuple(step_views),
        current_requirements=current_requirements,
        recovery_steps=tuple(recovery_views),
    )


def blocking_setup_url(
    workspace_state: WorkspaceState,
    requested_step: WorkspaceSetupStep,
) -> str | None:
    """Return the earliest incomplete page before ``requested_step``."""

    order = workspace_setup_step_order(workspace_state)
    try:
        requested_index = order.index(requested_step)
    except ValueError:
        return None
    requirements = workspace_setup_requirements(workspace_state)
    for step in order[:requested_index]:
        if any(item.step is step for item in requirements):
            return (
                f"{_setup_step_url(workspace_state.workspace_id, step)}"
                "?blocked=1#setup-blockers"
            )
    return None


def _setup_step_url(workspace_id: str, step: WorkspaceSetupStep) -> str:
    return f"/workspaces/{workspace_id}/{step.value}"

