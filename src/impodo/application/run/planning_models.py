"""Application projections produced while reviewing an integrated run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from impodo.application.recipe_application_service import RecipeApplicationAssessment
from impodo.domain.recipe.models import Recipe
from impodo.migration_run_planning import (
    MigrationRunPlanIssue,
    OdooModelRequirement,
    RecipeDependency,
    RecipeRevisionSelection,
    ReferenceRequirement,
)


@dataclass(frozen=True, slots=True)
class ReviewedRecipeApplication:
    """Retain one exact Recipe assessment inside an integrated-run review."""

    recipe: Recipe
    selection: RecipeRevisionSelection
    definition: Mapping[str, object]
    requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    write_claims: tuple[tuple[str, str], ...]
    assessment: RecipeApplicationAssessment


@dataclass(frozen=True, slots=True)
class IntegratedRunReview:
    """Show planning blockers before any run or workspace is created."""

    project_id: str
    data_version_id: str
    applications: tuple[ReviewedRecipeApplication, ...]
    dependencies: tuple[RecipeDependency, ...]
    model_requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    application_order: tuple[str, ...]
    planning_issues: tuple[MigrationRunPlanIssue, ...]

    @property
    def can_start(self) -> bool:
        return not any(item.blocks for item in self.planning_issues)
