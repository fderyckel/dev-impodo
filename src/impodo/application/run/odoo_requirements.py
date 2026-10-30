"""Project run-owned Odoo requirements from exact selected Recipe revisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from impodo.application.recipe.odoo_requirements import (
    OdooCheckModelRequirement,
    OdooCheckRelationshipRequirement,
    OdooCheckRequirementPlan,
    OdooCheckSupportingRequirement,
    SelectedRecipeRevision,
    extract_odoo_requirements,
)
from impodo.domain.run.contracts import RecipeRevisionSelection
from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability


class SelectedRunSetup(Protocol):
    @property
    def project_id(self) -> str: ...

    @property
    def selected_revisions(self) -> tuple[RecipeRevisionSelection, ...]: ...


class RunSetupSelectionReader(Protocol):
    """Read the Recipe selection that owns one setup workspace."""

    def for_workspace(self, workspace_id: str) -> SelectedRunSetup | None: ...


class SelectedRecipeRevisionReader(Protocol):
    """Bulk-read the exact Recipe revisions pinned by one run."""

    def read_revisions(
        self,
        project_id: str,
        revisions: tuple[tuple[str, int], ...],
        *,
        actor: Actor,
    ) -> Mapping[tuple[str, int], SelectedRecipeRevision]: ...


class RunOdooRequirementsUseCase:
    """Build an authorized, bounded Odoo requirement projection for either run."""

    def __init__(
        self,
        *,
        setups: RunSetupSelectionReader,
        recipes: SelectedRecipeRevisionReader,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._setups = setups
        self._recipes = recipes
        self._authorization = authorization

    def required_models_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[str, ...]:
        """Return the combined Recipe model scope for one setup workspace."""

        plan = self.for_workspace(workspace_id, actor=actor)
        return plan.model_names if plan is not None else ()

    def for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooCheckRequirementPlan | None:
        """Read all selected revisions once and combine their Odoo needs."""

        binding = self._setups.for_workspace(workspace_id)
        return self.for_selection(binding, actor=actor)

    def for_selection(
        self,
        binding: SelectedRunSetup | None,
        *,
        actor: Actor,
    ) -> OdooCheckRequirementPlan | None:
        """Verify Recipe requirements from a selection already read for this page."""

        if binding is None:
            return None
        self._authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self._recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        return extract_odoo_requirements(binding.selected_revisions, revisions)

__all__ = ["OdooCheckModelRequirement", "OdooCheckRelationshipRequirement", "OdooCheckRequirementPlan", "OdooCheckSupportingRequirement", "RunOdooRequirementsUseCase"]
