"""Resolve Recipe progress from durable milestones and current attempts.

Session progress may refine a saved milestone, but cannot undo later work or
unlock a dependency until verification has been recorded in the run registry.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from impodo.domain.run.contracts import (
    IntegratedRunBundle, RecipeApplicationStatus, RunRecipeApplication,
)
from impodo.application.workspace.preparation.job_models import PreparationJob
from impodo.domain.workspace.errors import WorkspaceError

if TYPE_CHECKING:
    from impodo.application.workspace.execution.job_models import LoadJob


class ApplicationResumeStep(StrEnum):
    """Name the work owned by the next application page."""

    PREPARE = "prepare"
    PREPARATION_PROGRESS = "preparation_progress"
    RESOLVE = "resolve"
    PREPARED_REVIEW = "prepared_review"
    LOAD_REVIEW = "load_review"
    LOAD_PROGRESS = "load_progress"
    LOAD_RESULT = "load_result"


def application_is_verified(application: RunRecipeApplication) -> bool:
    """Only a durable verified milestone releases the next Recipe."""

    return application.status in {
        RecipeApplicationStatus.RECONCILED, RecipeApplicationStatus.QUALIFIED,
    }


def ordered_applications(bundle: IntegratedRunBundle) -> tuple[RunRecipeApplication, ...]:
    """Apply the saved dependency order to the application's registry rows."""

    by_recipe = {item.recipe_id: item for item in bundle.applications}
    return tuple(
        by_recipe[recipe_id] for recipe_id in bundle.requirement_plan.application_order
    )


def next_unverified_application(bundle: IntegratedRunBundle) -> RunRecipeApplication | None:
    """Return the only application allowed to advance this run."""

    return next(
        (item for item in ordered_applications(bundle) if not application_is_verified(item)),
        None,
    )


def assert_application_is_current(
    bundle: IntegratedRunBundle, application: RunRecipeApplication,
) -> None:
    """Enforce dependency order for direct commands as well as the run page."""

    current = next_unverified_application(bundle)
    if current is not None and current.application_id != application.application_id:
        raise WorkspaceError("Finish and verify the earlier Recipe before continuing this one.")


def current_preparation(
    application: RunRecipeApplication, job: PreparationJob | None,
) -> PreparationJob | None:
    """Discard an attempt whose saved mapping has been replaced."""

    if job is None:
        return None
    mapping_hash = getattr(getattr(job, "workspace", None), "mapping_content_hash", None)
    if mapping_hash is not None and mapping_hash != application.mapping_content_hash:
        return None
    return job


def application_resume_step(
    application: RunRecipeApplication,
    preparation: PreparationJob | None = None,
    load: LoadJob | None = None,
) -> ApplicationResumeStep:
    """Prefer saved later work to older session snapshots after a restart."""

    if application_is_verified(application):
        return ApplicationResumeStep.LOAD_RESULT
    if load is not None and load.active:
        return ApplicationResumeStep.LOAD_PROGRESS
    if application.status is RecipeApplicationStatus.EXECUTED:
        return ApplicationResumeStep.LOAD_RESULT
    if load is not None:
        return (
            ApplicationResumeStep.LOAD_RESULT
            if load.status.value == "SUCCEEDED" else ApplicationResumeStep.LOAD_REVIEW
        )
    if application.status is RecipeApplicationStatus.COMPARED:
        return ApplicationResumeStep.LOAD_REVIEW
    preparation = current_preparation(application, preparation)
    if preparation is not None and preparation.active:
        return ApplicationResumeStep.PREPARATION_PROGRESS
    if preparation is not None and preparation.status.value == "REVIEW_REQUIRED":
        return ApplicationResumeStep.RESOLVE
    if application.status is RecipeApplicationStatus.PREPARED or (
        preparation is not None and preparation.status.value == "SUCCEEDED"
    ):
        return ApplicationResumeStep.PREPARED_REVIEW
    return ApplicationResumeStep.PREPARE
