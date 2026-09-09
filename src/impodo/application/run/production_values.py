"""Review and validate fresh Production values against qualified Recipe meaning."""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from impodo.domain.cutover.models import CutoverPlanRevision
from impodo.domain.data_version.models import DataVersion
from impodo.domain.recipe.models import RecipeError
from impodo.domain.recipe.control_values import normalize_recipe_control_values
from impodo.domain.recipe_parameters import EXPORT_AS_OF_PARAMETER_ID, normalize_recipe_parameter_values
from impodo.domain.run.production import ProductionRunBinding, ProductionRunError
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from .fresh_data_matching import FreshDataRecipeRequirement
from .fresh_data_values import (
    FreshDataActivationValues, FreshDataRunValuePlan, build_fresh_data_run_value_plan,
    control_definitions, fresh_control_requirements, fresh_input_requirements,
    fresh_parameter_requirements, normalize_export_date, parameter_definitions, recipe_definition,
)


@dataclass(frozen=True, slots=True)
class ProductionValueReview:
    """Bind the displayed prompts to the exact selected plan and delivery."""

    evidence_hash: str
    definitions: Mapping[str, Mapping[str, object]]
    export_as_of: str
    values: FreshDataRunValuePlan

    def with_answers(
        self, parameters: Mapping[str, object], controls: Mapping[str, Mapping[str, object]],
    ) -> FreshDataRunValuePlan:
        """Retain editable answers when a request needs correction."""

        values = tuple(
            replace(item, supplied_value=str(parameters[item.logical_parameter_id]))
            if not item.automatic and item.logical_parameter_id in parameters else item
            for item in self.values.values
        )
        totals = []
        for item in self.values.controls:
            answers = controls.get(item.recipe_id, {})
            if not item.automatic and item.requirement.logical_control_id in answers:
                item = replace(item, supplied_value=str(answers[item.requirement.logical_control_id]))
            totals.append(item)
        return replace(self.values, values=values, controls=tuple(totals))


class ProductionRunValuesUseCase:
    """Reuse Test's typed prompts and the canonical Recipe value validators."""

    def __init__(self, recipes) -> None:
        self._recipes = recipes

    def review(
        self, binding: ProductionRunBinding, plan: CutoverPlanRevision, data_version: DataVersion,
        *, actor: Actor,
    ) -> ProductionValueReview:
        if (
            binding.plan_content_hash != plan.content_hash
            or binding.project_id != plan.project_id
            or binding.project_id != data_version.project_id
            or binding.data_version_id != data_version.data_version_id
        ):
            raise ProductionRunError("Production values no longer match the selected plan and delivery")
        revisions = self._recipes.read_revisions(
            binding.project_id,
            tuple((item.recipe_id, item.recipe_revision) for item in plan.selected_revisions),
            actor=actor,
        )
        requirements = []
        definitions = {}
        for selection in plan.selected_revisions:
            revision = revisions[(selection.recipe_id, selection.recipe_revision)]
            if revision.envelope.get("semantic_hash") != selection.semantic_hash:
                raise RecipeError("The selected Recipe version has changed")
            definition = recipe_definition(revision.envelope)
            definitions[selection.recipe_id] = definition
            requirements.append(FreshDataRecipeRequirement(
                recipe_id=selection.recipe_id, recipe_revision=selection.recipe_revision,
                display_name=revision.recipe.display_name, business_purpose=revision.recipe.business_purpose,
                inputs=fresh_input_requirements(definition),
                parameters=fresh_parameter_requirements(definition, data_version.export_as_of),
                controls=fresh_control_requirements(definition),
            ))
        return ProductionValueReview(
            evidence_hash=content_hash({
                "binding": binding.content_hash, "plan": plan.content_hash,
                "data_version": data_version.data_version_id,
                "source_package": data_version.source_package_hash,
                "export_as_of": data_version.export_as_of,
            }),
            definitions=definitions, export_as_of=normalize_export_date(data_version.export_as_of),
            values=build_fresh_data_run_value_plan(tuple(requirements), None),
        )

    @staticmethod
    def normalize(
        review: ProductionValueReview,
        parameters: Mapping[str, Mapping[str, object]] | None,
        controls: Mapping[str, Mapping[str, object]] | None,
    ) -> FreshDataActivationValues:
        """Reject undeclared answers and derive fixed values from this delivery."""

        parameters, controls = parameters or {}, controls or {}
        if (set(parameters) | set(controls)) - set(review.definitions):
            raise ProductionRunError("Production values must belong to the selected Recipes")
        conflicts = tuple(item.conflict for item in review.values.values if item.conflict)
        if conflicts:
            raise ProductionRunError(conflicts[0])
        normalized_parameters, normalized_controls = {}, {}
        for recipe_id, definition in review.definitions.items():
            declarations = parameter_definitions(definition)
            supplied = dict(parameters.get(recipe_id, {}))
            if any(item.get("logical_parameter_id") == EXPORT_AS_OF_PARAMETER_ID for item in declarations):
                if EXPORT_AS_OF_PARAMETER_ID in supplied and supplied[EXPORT_AS_OF_PARAMETER_ID] != review.export_as_of:
                    raise ProductionRunError("The export date is fixed by this Production delivery")
                supplied[EXPORT_AS_OF_PARAMETER_ID] = review.export_as_of
            values = normalize_recipe_parameter_values(declarations, supplied)
            totals = normalize_recipe_control_values(
                control_definitions(definition), controls.get(recipe_id, {}), require_all=True,
            )
            if values:
                normalized_parameters[recipe_id] = values
            if totals:
                normalized_controls[recipe_id] = totals
        return FreshDataActivationValues(parameters=normalized_parameters, controls=normalized_controls)

    def submitted_values(
        self, review: ProductionValueReview, parameters: Mapping[str, object],
        controls: Mapping[str, Mapping[str, object]], *, expected_evidence_hash: str,
    ) -> FreshDataActivationValues:
        """Expand shared answers only after checking the displayed evidence."""

        if expected_evidence_hash != review.evidence_hash:
            raise ProductionRunError("Production setup changed. Reload and review its current values.")
        allowed = {item.logical_parameter_id for item in review.values.editable_values}
        if set(parameters) - allowed:
            raise ProductionRunError("A submitted Production value is not editable")
        editable_controls = {}
        for item in review.values.editable_controls:
            editable_controls.setdefault(item.recipe_id, set()).add(item.requirement.logical_control_id)
        if set(controls) - set(editable_controls) or any(
            set(values) - editable_controls[recipe_id] for recipe_id, values in controls.items()
        ):
            raise ProductionRunError("A submitted Production total is not editable")
        by_recipe = {}
        for item in review.values.editable_values:
            for recipe_id in item.recipe_ids:
                by_recipe.setdefault(recipe_id, {})[item.logical_parameter_id] = parameters.get(item.logical_parameter_id, "")
        return self.normalize(review, by_recipe, controls)
