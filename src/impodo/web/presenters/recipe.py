"""Present Recipe authoring recovery without duplicating compiler rules."""

from __future__ import annotations

from dataclasses import dataclass

from ...projects import MigrationProject, ProjectStatus
from ...recipes import RecipeDraftIssue, RecipeDraftRecoveryStep


@dataclass(frozen=True, slots=True)
class RecipeDraftRecoveryView:
    """Give the operator one named action for one structured blocker."""

    message: str
    recovery_action: str
    href: str
    action_label: str
    support_reference: str


_PROJECT_RECOVERY = {
    RecipeDraftRecoveryStep.PROJECT_SETUP: ("details", "Complete Recipe setup"),
    RecipeDraftRecoveryStep.SOURCE_DATA: ("datasets", "Review Source data"),
    RecipeDraftRecoveryStep.ODOO_DATA: ("schema", "Review Odoo data"),
    RecipeDraftRecoveryStep.MATCH_DATA: ("mapping", "Review Match data"),
    RecipeDraftRecoveryStep.PREPARE_DATA: ("prepare", "Review Prepare data"),
}


def build_recipe_draft_recovery_view(
    recipe_id: str,
    project: MigrationProject | None,
    issue: RecipeDraftIssue,
) -> RecipeDraftRecoveryView:
    """Resolve the compiler-owned recovery step to an existing page."""

    if project is not None and project.status is not ProjectStatus.REGISTERED:
        href = f"/projects/{project.project_id}/details"
        action_label = "Complete Recipe setup"
    elif issue.recovery_step in _PROJECT_RECOVERY and project is not None:
        page, action_label = _PROJECT_RECOVERY[issue.recovery_step]
        href = f"/projects/{project.project_id}/{page}"
    elif issue.recovery_step is RecipeDraftRecoveryStep.RECIPE_APPLICATION:
        href = f"/recipes/{recipe_id}/application"
        action_label = "Review application"
    elif issue.recovery_step is RecipeDraftRecoveryStep.NEW_PROJECT:
        href = "/recipes/new"
        action_label = "Create a new project"
    else:
        href = f"/recipes/{recipe_id}"
        action_label = "Review Recipe"

    return RecipeDraftRecoveryView(
        message=issue.message,
        recovery_action=issue.recovery_action,
        href=href,
        action_label=action_label,
        support_reference=issue.support_reference,
    )
