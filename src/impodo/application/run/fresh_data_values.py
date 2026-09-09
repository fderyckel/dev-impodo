"""Parse and merge Recipe-declared values for a fresh Test or Production delivery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from ...domain.recipe.models import RecipeError
from ...domain.serialization import canonical_json
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationFoundationError,
)
from impodo.domain.run.test_setup import TestRunValues, TestRunSetupBinding
from impodo.domain.recipe.control_values import normalize_control_total
from .fresh_data_matching import (
    FreshDataInputRequirement,
    FreshDataControlRequirement,
    FreshDataParameterRequirement,
    FreshDataRecipeRequirement,
)


@dataclass(frozen=True, slots=True)
class FreshDataRunValue:
    """Present one shared answer requested by one or more selected Recipes."""

    logical_parameter_id: str
    label: str
    value_type: str
    required: bool
    constraints: Mapping[str, object]
    recipe_ids: tuple[str, ...]
    recipe_names: tuple[str, ...]
    supplied_value: str | None
    automatic: bool
    conflict: str | None = None

    @property
    def input_type(self) -> str:
        return {
            "date": "date",
            "decimal": "number",
            "integer": "number",
        }.get(self.value_type, "text")

    @property
    def input_step(self) -> str | None:
        if self.value_type == "decimal":
            return "any"
        if self.value_type == "integer":
            return "1"
        return None

    @property
    def max_length(self) -> int | None:
        value = self.constraints.get("max_length")
        return int(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class FreshDataControlValue:
    """Present one Recipe's expected total for this delivery."""

    recipe_id: str
    recipe_name: str
    requirement: FreshDataControlRequirement
    supplied_value: str | None

    @property
    def automatic(self) -> bool:
        return self.requirement.invariant_total is not None


@dataclass(frozen=True, slots=True)
class FreshDataRunValuePlan:
    """Hold every Recipe-declared run value and its current confirmation."""

    values: tuple[FreshDataRunValue, ...]
    revision: int | None
    confirmed: bool
    controls: tuple[FreshDataControlValue, ...] = ()

    @property
    def has_values(self) -> bool:
        return bool(self.values or self.controls)

    @property
    def editable_controls(self) -> tuple[FreshDataControlValue, ...]:
        return tuple(item for item in self.controls if not item.automatic)

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.editable_values or self.editable_controls)

    @property
    def editable_values(self) -> tuple[FreshDataRunValue, ...]:
        return tuple(item for item in self.values if not item.automatic)

    @property
    def automatic_values(self) -> tuple[FreshDataRunValue, ...]:
        return tuple(item for item in self.values if item.automatic)

    @property
    def ready_to_continue(self) -> bool:
        if not self.can_confirm:
            return False
        if any(item.required and not item.supplied_value for item in self.values):
            return False
        if any(item.supplied_value is None for item in self.controls):
            return False
        return not self.requires_confirmation or self.confirmed

    @property
    def can_confirm(self) -> bool:
        if any(item.conflict for item in self.values):
            return False
        return not any(
            item.automatic and item.required and not item.supplied_value
            for item in self.values
        )


@dataclass(frozen=True, slots=True)
class FreshDataActivationValues:
    """Pass separately validated parameters and totals to the existing compiler."""

    parameters: dict[str, dict[str, object]]
    controls: dict[str, dict[str, str]]


def recipe_definition(envelope: Mapping[str, object]) -> Mapping[str, object]:
    """Read one portable Recipe definition from a stored revision envelope."""

    definition = envelope.get("recipe")
    if not isinstance(definition, Mapping):
        raise RecipeError("Stored Recipe source requirements are invalid")
    return definition


def parameter_definitions(
    definition: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Read and validate the Recipe's declared run-value shapes."""

    payload = definition.get("parameter_definitions", {})
    if not isinstance(payload, Mapping):
        raise RecipeError("Stored Recipe run values are invalid")
    parameters = payload.get("parameters", ())
    if not isinstance(parameters, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in parameters
    ):
        raise RecipeError("Stored Recipe run values are invalid")
    return tuple(parameters)


def fresh_parameter_requirements(
    definition: Mapping[str, object],
    export_as_of: object,
) -> tuple[FreshDataParameterRequirement, ...]:
    """Project Recipe parameter declarations into fresh-data prompts."""

    return tuple(
        FreshDataParameterRequirement(
            logical_parameter_id=str(item.get("logical_parameter_id", "")),
            label=str(item.get("label", "Run value")),
            value_type=str(item.get("type", "string")),
            required=bool(item.get("required", False)),
            constraints=dict(item.get("constraints", {})),
            supplied_value=(
                normalize_export_date(export_as_of)
                if item.get("logical_parameter_id")
                == "parameter:export_as_of_date"
                else None
            ),
        )
        for item in parameter_definitions(definition)
    )


def fresh_input_requirements(
    definition: Mapping[str, object],
) -> tuple[FreshDataInputRequirement, ...]:
    """Project Recipe source declarations into required table shapes."""

    source_shape = definition.get("source_shape", {})
    if not isinstance(source_shape, Mapping):
        raise RecipeError("Stored Recipe source requirements are invalid")
    datasets = source_shape.get("datasets", ())
    if not isinstance(datasets, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in datasets
    ):
        raise RecipeError("Stored Recipe source requirements are invalid")
    result = []
    for dataset in datasets:
        columns = dataset.get("columns", ())
        if not isinstance(columns, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in columns
        ):
            raise RecipeError("Stored Recipe source requirements are invalid")
        result.append(
            FreshDataInputRequirement(
                logical_dataset_id=str(dataset.get("logical_dataset_id", "")),
                label=str(dataset.get("logical_name", "Required table")),
                columns=tuple(
                    str(column.get("source_name", "Required column"))
                    for column in columns
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.logical_dataset_id))


def control_definitions(definition: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """Read control declarations from a verified Recipe envelope."""

    payload = definition.get("control_definitions", {})
    if not isinstance(payload, Mapping):
        raise RecipeError("Stored Recipe control totals are invalid")
    controls = payload.get("controls", ())
    if not isinstance(controls, (list, tuple)) or any(not isinstance(item, Mapping) for item in controls):
        raise RecipeError("Stored Recipe control totals are invalid")
    return tuple(controls)


def fresh_control_requirements(definition: Mapping[str, object]) -> tuple[FreshDataControlRequirement, ...]:
    """Keep reusable controls visible and request only delivery-owned totals."""

    datasets = {item.logical_dataset_id: item.label for item in fresh_input_requirements(definition)}
    return tuple(
        FreshDataControlRequirement(
            logical_control_id=str(item["logical_control_id"]),
            label=str(item["name"]),
            dataset_label=datasets.get(str(item["dataset_id"]), str(item["dataset_id"])),
            unit=str(item.get("unit", "")),
            tolerance=str(item.get("tolerance", "0")),
            invariant_total=(
                normalize_control_total(item["invariant_expected_total"], label=str(item["name"]))
                if item.get("invariant_expectation") else None
            ),
        )
        for item in sorted(control_definitions(definition), key=lambda item: str(item["logical_control_id"]))
    )


def build_fresh_data_run_value_plan(
    requirements: tuple[FreshDataRecipeRequirement, ...],
    current: TestRunValues | None,
) -> FreshDataRunValuePlan:
    """Merge compatible Recipe requests so the data manager answers once."""

    stored = current.by_recipe if current is not None else {}
    stored_controls = current.controls_by_recipe if current is not None else {}
    grouped: dict[
        str,
        list[tuple[FreshDataRecipeRequirement, FreshDataParameterRequirement]],
    ] = {}
    for recipe in requirements:
        for parameter in recipe.parameters:
            grouped.setdefault(parameter.logical_parameter_id, []).append(
                (recipe, parameter)
            )

    values: list[FreshDataRunValue] = []
    for logical_id, uses in grouped.items():
        first = uses[0][1]
        signature = canonical_json(
            {
                "constraints": dict(first.constraints),
                "required": first.required,
                "type": first.value_type,
            }
        )
        conflict = None
        if any(
            canonical_json(
                {
                    "constraints": dict(parameter.constraints),
                    "required": parameter.required,
                    "type": parameter.value_type,
                }
            )
            != signature
            for _recipe, parameter in uses[1:]
        ):
            conflict = (
                "Selected Recipes disagree about the meaning of "
                f"{first.label}. Use a plan with compatible "
                "Recipe versions."
            )

        automatic_flags = {
            parameter.supplied_value is not None for _recipe, parameter in uses
        }
        if len(automatic_flags) != 1:
            conflict = (
                "Selected Recipes disagree about who supplies "
                f"{first.label}. Use a plan with compatible "
                "Recipe versions."
            )
        automatic = automatic_flags == {True}
        candidate_values: list[object] = []
        for recipe, parameter in uses:
            if automatic:
                candidate_values.append(parameter.supplied_value)
            elif logical_id in stored.get(recipe.recipe_id, {}):
                candidate_values.append(stored[recipe.recipe_id][logical_id])
        if candidate_values and (
            len(candidate_values) != len(uses)
            or any(value != candidate_values[0] for value in candidate_values[1:])
        ):
            conflict = f"Saved answers for {first.label} are inconsistent"
        supplied_value = str(candidate_values[0]) if candidate_values else None
        values.append(
            FreshDataRunValue(
                logical_parameter_id=logical_id,
                label=first.label,
                value_type=first.value_type,
                required=first.required,
                constraints=dict(first.constraints),
                recipe_ids=tuple(recipe.recipe_id for recipe, _item in uses),
                recipe_names=tuple(recipe.display_name for recipe, _item in uses),
                supplied_value=supplied_value,
                automatic=automatic,
                conflict=conflict,
            )
        )
    return FreshDataRunValuePlan(
        values=tuple(values),
        revision=current.revision if current is not None else None,
        confirmed=current is not None,
        controls=tuple(
            FreshDataControlValue(
                recipe_id=recipe.recipe_id,
                recipe_name=recipe.display_name,
                requirement=control,
                supplied_value=(
                    control.invariant_total if control.invariant_total is not None else
                    stored_controls.get(recipe.recipe_id, {}).get(control.logical_control_id)
                ),
            )
            for recipe in requirements for control in recipe.controls
        ),
    )


def assert_run_value_ownership(
    binding: TestRunSetupBinding,
    current: TestRunValues | None,
) -> None:
    """Reject saved values belonging to another Test setup aggregate."""

    if current is not None and (
        current.test_run_setup_id != binding.test_run_setup_id
        or current.project_id != binding.project_id
        or current.migration_run_id != binding.migration_run_id
        or (
            current.contract_version == 2
            and current.recipe_revisions != binding.selected_revisions
        )
    ):
        raise MigrationConflictError(
            "Saved run values do not belong to this Test setup"
        )


def normalize_export_date(export_as_of: object) -> str:
    """Return the canonical date prefix used by automatic run parameters."""

    candidate = str(export_as_of).strip()[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as error:
        raise MigrationFoundationError(
            "The delivery cutoff must start with a year-month-day date"
        ) from error
