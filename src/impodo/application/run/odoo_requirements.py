"""Project run-owned Odoo requirements from exact selected Recipe revisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.recipe.models import RecipeError
from impodo.domain.run.test_setup import TestRunSetupBinding

from .fresh_data_values import recipe_definition


class TestRunSelectionReader(Protocol):
    """Read the Test setup selection that owns one setup workspace."""

    def for_workspace(self, workspace_id: str) -> TestRunSetupBinding | None: ...


class SelectedRecipe(Protocol):
    """Expose only the Recipe presentation needed by this query."""

    display_name: str


class SelectedRecipeRevision(Protocol):
    """Expose one verified Recipe envelope and its bounded presentation."""

    envelope: Mapping[str, object]
    recipe: SelectedRecipe


class SelectedRecipeRevisionReader(Protocol):
    """Bulk-read the exact Recipe revisions pinned by one Test run."""

    def read_revisions(
        self,
        project_id: str,
        revisions: tuple[tuple[str, int], ...],
        *,
        actor: Actor,
    ) -> Mapping[tuple[str, int], SelectedRecipeRevision]: ...


@dataclass(frozen=True, slots=True)
class OdooCheckModelRequirement:
    """Present one Recipe-derived Odoo model and its exact field scope."""

    model_name: str
    field_names: tuple[str, ...]
    recipe_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OdooCheckRelationshipRequirement:
    """Retain one Recipe-owned path to a supporting Odoo record type."""

    parent_model: str
    relationship_field: str
    relationship_type: str


@dataclass(frozen=True, slots=True)
class OdooCheckSupportingRequirement:
    """Present one current Odoo value set required by selected Recipes."""

    model_name: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    relationships: tuple[OdooCheckRelationshipRequirement, ...]
    recipe_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OdooCheckRequirementPlan:
    """Combine every selected Recipe's target needs before contacting Odoo."""

    models: tuple[OdooCheckModelRequirement, ...]
    supporting_values: tuple[OdooCheckSupportingRequirement, ...]

    @property
    def model_names(self) -> tuple[str, ...]:
        return tuple(item.model_name for item in self.models)


class TestRunOdooRequirementsUseCase:
    """Build an authorized, bounded Odoo requirement projection for a Test run."""

    def __init__(
        self,
        *,
        test_runs: TestRunSelectionReader,
        recipes: SelectedRecipeRevisionReader,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._test_runs = test_runs
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

        binding = self._test_runs.for_workspace(workspace_id)
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
        model_fields: dict[str, set[str]] = {}
        model_recipes: dict[str, set[str]] = {}
        supporting_recipes: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]], set[str]
        ] = {}
        supporting_paths: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]],
            set[tuple[str, str, str]],
        ] = {}
        for selection in binding.selected_revisions:
            revision = revisions[(selection.recipe_id, selection.recipe_revision)]
            selected_hash = str(revision.envelope.get("semantic_hash", ""))
            if selected_hash != selection.semantic_hash:
                raise RecipeError("The selected Recipe version has changed")
            definition = recipe_definition(revision.envelope)
            recipe_name = revision.recipe.display_name
            contract = dict(definition.get("odoo_target_contract", {}))
            for model in contract.get("models", ()):  # type: ignore[union-attr]
                model_definition = dict(model)
                model_name = str(model_definition.get("model", "")).strip()
                if model_name:
                    model_fields.setdefault(model_name, set()).update(
                        str(dict(field).get("name", "")).strip()
                        for field in model_definition.get("fields", ())
                        if str(dict(field).get("name", "")).strip()
                    )
                    model_recipes.setdefault(model_name, set()).add(recipe_name)
                for raw_path in model_definition.get("reference_paths", ()):
                    path = dict(raw_path)
                    key_fields = tuple(
                        str(value).strip()
                        for value in path.get("key_fields", ())
                        if str(value).strip()
                    )
                    scope_fields = tuple(
                        str(value).strip()
                        for value in path.get("scope_fields", ())
                        if str(value).strip()
                    )
                    parent_model = str(path.get("parent_model", "")).strip()
                    relationship_field = str(path.get("relationship_field", "")).strip()
                    relationship_type = str(path.get("relationship_type", "")).strip()
                    if not (
                        model_name
                        and key_fields
                        and parent_model
                        and relationship_field
                        and relationship_type
                    ):
                        message = (
                            "A selected Recipe contains an incomplete Odoo relationship"
                        )
                        raise RecipeError(message)
                    identity = (model_name, key_fields, scope_fields)
                    supporting_recipes.setdefault(identity, set()).add(recipe_name)
                    supporting_paths.setdefault(identity, set()).add(
                        (
                            parent_model,
                            relationship_field,
                            relationship_type,
                        )
                    )
        return OdooCheckRequirementPlan(
            models=tuple(
                OdooCheckModelRequirement(
                    model_name=model_name,
                    field_names=tuple(sorted(model_fields[model_name])),
                    recipe_names=tuple(
                        sorted(model_recipes[model_name], key=str.casefold)
                    ),
                )
                for model_name in sorted(model_fields)
            ),
            supporting_values=tuple(
                OdooCheckSupportingRequirement(
                    model_name=identity[0],
                    key_fields=identity[1],
                    scope_fields=identity[2],
                    relationships=tuple(
                        OdooCheckRelationshipRequirement(
                            parent_model=parent_model,
                            relationship_field=relationship_field,
                            relationship_type=relationship_type,
                        )
                        for (
                            parent_model,
                            relationship_field,
                            relationship_type,
                        ) in sorted(supporting_paths[identity])
                    ),
                    recipe_names=tuple(
                        sorted(supporting_recipes[identity], key=str.casefold)
                    ),
                )
                for identity in sorted(supporting_recipes)
            ),
        )
