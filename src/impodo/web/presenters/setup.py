"""Present draft setup readiness without duplicating lifecycle rules."""

from __future__ import annotations

from dataclasses import dataclass

from ...projects import (
    MigrationProject,
    ProjectSetupRequirement,
    ProjectSetupStep,
    SourceMode,
    project_setup_requirements,
)


@dataclass(frozen=True, slots=True)
class ProjectSetupStepView:
    """Render one compact step in the draft setup navigation."""

    step_id: str
    label: str
    href: str | None
    current: bool
    status: str
    status_label: str
    requirements: tuple[ProjectSetupRequirement, ...]


@dataclass(frozen=True, slots=True)
class ProjectSetupRecoveryView:
    """Group exceptional confirmation blockers by their recovery page."""

    step_id: str
    label: str
    href: str
    requirements: tuple[ProjectSetupRequirement, ...]


@dataclass(frozen=True, slots=True)
class ProjectSetupView:
    """Provide the setup navigation and the current page's useful blockers."""

    steps: tuple[ProjectSetupStepView, ...]
    current_requirements: tuple[ProjectSetupRequirement, ...]
    recovery_steps: tuple[ProjectSetupRecoveryView, ...]


_STEP_LABELS = {
    ProjectSetupStep.DETAILS: "Project",
    ProjectSetupStep.GOVERNANCE: "People",
    ProjectSetupStep.FILES: "Source files",
    ProjectSetupStep.TARGET: "Connect Odoo",
    ProjectSetupStep.REVIEW: "Confirm",
}

_TEMPLATE_STEPS = {
    "project_details.html": ProjectSetupStep.DETAILS,
    "project_governance.html": ProjectSetupStep.GOVERNANCE,
    "project_files.html": ProjectSetupStep.FILES,
    "project_target.html": ProjectSetupStep.TARGET,
    "project_review.html": ProjectSetupStep.REVIEW,
}


def project_setup_step_order(
    project: MigrationProject,
) -> tuple[ProjectSetupStep, ...]:
    """Return the setup sequence for the draft's selected source mode."""

    steps = [ProjectSetupStep.DETAILS, ProjectSetupStep.GOVERNANCE]
    if project.source_mode is SourceMode.FILE:
        steps.append(ProjectSetupStep.FILES)
    steps.extend((ProjectSetupStep.TARGET, ProjectSetupStep.REVIEW))
    return tuple(steps)


def build_project_setup_view(
    project: MigrationProject,
    template_name: str,
) -> ProjectSetupView:
    """Build one request-scoped setup view from the current project only."""

    requirements = project_setup_requirements(project)
    current_step = _TEMPLATE_STEPS.get(template_name)
    order = project_setup_step_order(project)
    step_views: list[ProjectSetupStepView] = []
    recovery_views: list[ProjectSetupRecoveryView] = []
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
            href = _setup_step_url(project.project_id, step)
        elif step_requirements:
            status = "attention"
            status_label = "Needs attention"
            href = _setup_step_url(project.project_id, step)
        else:
            status = "complete"
            status_label = "Complete"
            href = _setup_step_url(project.project_id, step)

        step_views.append(
            ProjectSetupStepView(
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
                ProjectSetupRecoveryView(
                    step_id=step.value,
                    label=_STEP_LABELS[step],
                    href=_setup_step_url(project.project_id, step),
                    requirements=step_requirements,
                )
            )
            earlier_steps_complete = False

    current_requirements = tuple(
        item for item in requirements if item.step is current_step
    )
    return ProjectSetupView(
        steps=tuple(step_views),
        current_requirements=current_requirements,
        recovery_steps=tuple(recovery_views),
    )


def blocking_setup_url(
    project: MigrationProject,
    requested_step: ProjectSetupStep,
) -> str | None:
    """Return the earliest incomplete page before ``requested_step``."""

    order = project_setup_step_order(project)
    try:
        requested_index = order.index(requested_step)
    except ValueError:
        return None
    requirements = project_setup_requirements(project)
    for step in order[:requested_index]:
        if any(item.step is step for item in requirements):
            return (
                f"{_setup_step_url(project.project_id, step)}"
                "?blocked=1#setup-blockers"
            )
    return None


def _setup_step_url(project_id: str, step: ProjectSetupStep) -> str:
    return f"/projects/{project_id}/{step.value}"
