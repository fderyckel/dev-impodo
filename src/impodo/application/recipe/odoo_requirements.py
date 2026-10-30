"""Extract portable Odoo requirements from already bulk-loaded Recipe revisions.

The caller authorizes and bulk-loads exact revisions before invoking this pure
query. It owns no run setup, database, network reader, or authorization.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from impodo.domain.recipe.models import RecipeError
from impodo.domain.run.contracts import RecipeRevisionSelection
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.supporting_closure import recipe_supporting_choices
from impodo.domain.workspace.supporting_review import SupportingChoice


class SelectedRecipe(Protocol):
    """Expose only the Recipe presentation needed by this query."""

    display_name: str


class SelectedRecipeRevision(Protocol):
    """Expose one verified Recipe envelope and its bounded presentation."""

    envelope: Mapping[str, object]
    recipe: SelectedRecipe


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
class RecipeSupportingOrigin:
    """Attribute one relationship reason to its exact Recipe revision."""

    parent_model: str
    relationship_field: str
    relationship_type: str
    recipe_id: str
    recipe_revision: int
    semantic_hash: str
    recipe_name: str


@dataclass(frozen=True, slots=True)
class OdooCheckSupportingRequirement:
    """Present one current Odoo value set required by selected Recipes."""

    model_name: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    relationships: tuple[OdooCheckRelationshipRequirement, ...]
    recipe_names: tuple[str, ...]
    origins: tuple[RecipeSupportingOrigin, ...] = ()


@dataclass(frozen=True, slots=True)
class RecipeSupportingDecision:
    """Keep reviewed portable meaning and the exact selected revision together."""

    choice: SupportingChoice
    recipe_id: str
    recipe_revision: int
    semantic_hash: str
    recipe_name: str


@dataclass(frozen=True, slots=True)
class OdooCheckRequirementPlan:
    """Combine every selected Recipe's target needs before contacting Odoo."""

    models: tuple[OdooCheckModelRequirement, ...]
    supporting_values: tuple[OdooCheckSupportingRequirement, ...]
    selected_revisions: tuple[RecipeRevisionSelection, ...] = ()
    decisions: tuple[RecipeSupportingDecision, ...] = ()

    @property
    def content_hash(self) -> str:
        """Bind reasons and exact revisions without including target access state."""
        return content_hash(
            {
                "models": [asdict(item) for item in self.models],
                "supporting_values": [asdict(item) for item in self.supporting_values],
                "decisions": [asdict(item) for item in self.decisions],
                "selected_revisions": [
                    (item.recipe_id, item.recipe_revision, item.semantic_hash)
                    for item in self.selected_revisions
                ],
            }
        )

    @property
    def model_names(self) -> tuple[str, ...]:
        return tuple(item.model_name for item in self.models)


def extract_odoo_requirements(
    selections: tuple[RecipeRevisionSelection, ...],
    revisions: Mapping[tuple[str, int], SelectedRecipeRevision],
) -> OdooCheckRequirementPlan:
    """Verify selected hashes and retain every distinct originating relationship."""
    model_fields: dict[str, set[str]] = {}
    model_recipes: dict[str, set[str]] = {}
    supporting_recipes: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]], set[str]
    ] = {}
    supporting_paths: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]],
        set[tuple[str, str, str]],
    ] = {}
    origins: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]],
        dict[tuple[str, str, str, str, int, str], RecipeSupportingOrigin],
    ] = {}
    decisions = []
    by_edge: dict[tuple[str, str], SupportingChoice] = {}
    for selection in selections:
        revision = revisions.get((selection.recipe_id, selection.recipe_revision))
        if revision is None:
            raise RecipeError("The selected Recipe version is unavailable")
        selected_hash = str(revision.envelope.get("semantic_hash", ""))
        if selected_hash != selection.semantic_hash:
            raise RecipeError("The selected Recipe version has changed")
        definition = _mapping(revision.envelope.get("recipe"), "definition")
        recipe_name = revision.recipe.display_name
        raw_contract = definition.get("odoo_target_contract", {})
        if not isinstance(raw_contract, Mapping):
            raise RecipeError("The selected Recipe target contract is invalid")
        contract = dict(raw_contract)
        try:
            choices = recipe_supporting_choices(contract)
        except ValueError as error:
            raise RecipeError(str(error)) from error
        for choice in choices:
            edge = (choice.source_model, choice.field_name)
            prior = by_edge.get(edge)
            if prior is not None and prior != choice:
                raise RecipeError(
                    f"Selected Recipes disagree about supporting data for {edge[0]}.{edge[1]}"
                )
            by_edge[edge] = choice
            decisions.append(
                RecipeSupportingDecision(
                    choice,
                    selection.recipe_id,
                    selection.recipe_revision,
                    selection.semantic_hash,
                    recipe_name,
                )
            )
        for model in _sequence(contract.get("models", ()), "models"):
            model_definition = _mapping(model, "model")
            model_name = str(model_definition.get("model", "")).strip()
            if model_name:
                model_fields.setdefault(model_name, set()).update(
                    str(_mapping(field, "field").get("name", "")).strip()
                    for field in _sequence(model_definition.get("fields", ()), "fields")
                    if str(_mapping(field, "field").get("name", "")).strip()
                )
                model_recipes.setdefault(model_name, set()).add(recipe_name)
            for raw_path in _sequence(
                model_definition.get("reference_paths", ()), "reference paths"
            ):
                path = _mapping(raw_path, "reference path")
                key_fields = tuple(
                    str(value).strip()
                    for value in _sequence(path.get("key_fields", ()), "key fields")
                    if str(value).strip()
                )
                scope_fields = tuple(
                    str(value).strip()
                    for value in _sequence(path.get("scope_fields", ()), "scope fields")
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
                origin_key = (
                    parent_model,
                    relationship_field,
                    relationship_type,
                    selection.recipe_id,
                    selection.recipe_revision,
                    selection.semantic_hash,
                )
                origins.setdefault(identity, {})[origin_key] = RecipeSupportingOrigin(
                    *origin_key, recipe_name
                )
    return OdooCheckRequirementPlan(
        selected_revisions=selections,
        decisions=tuple(decisions),
        models=tuple(
            OdooCheckModelRequirement(
                model_name=model_name,
                field_names=tuple(sorted(model_fields[model_name])),
                recipe_names=tuple(sorted(model_recipes[model_name], key=str.casefold)),
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
                origins=tuple(
                    origins[identity][key] for key in sorted(origins[identity])
                ),
            )
            for identity in sorted(supporting_recipes)
        ),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RecipeError(f"The selected Recipe {label} is invalid")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 10_000:
        raise RecipeError(
            f"The selected Recipe {label} is invalid or exceeds its limit"
        )
    return tuple(value)
