"""Coordinate fresh source requirements and run-owned values for Test setup."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.data_version.models import DataVersionState
from impodo.domain.recipe.models import RecipeError
from impodo.domain.recipe_parameters import (
    EXPORT_AS_OF_PARAMETER_ID,
    RecipeParameterValueError,
    normalize_recipe_parameter_values,
)
from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    utc_now,
)
from impodo.domain.run.test_setup import (
    RecipeRunParameterValue,
    TestRunParameterValues,
    TestRunSetupBinding,
)

from .fresh_data_matching import (
    FreshDataMatchPlan,
    FreshDataRecipeRequirement,
    build_fresh_data_match_plan,
)
from .fresh_data_values import (
    FreshDataRunValuePlan,
    assert_run_value_ownership,
    build_fresh_data_run_value_plan,
    fresh_input_requirements,
    fresh_parameter_requirements,
    normalize_export_date,
    parameter_definitions,
    recipe_definition,
)


class TestRunFreshDataUseCase:
    """Own the bounded Fresh data query and its optimistic run-value command."""

    def __init__(
        self,
        *,
        data_versions,
        recipes,
        test_runs,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._data_versions = data_versions
        self._recipes = recipes
        self._test_runs = test_runs
        self._authorization = authorization

    def requirements(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> tuple[FreshDataRecipeRequirement, ...]:
        """Return Recipe-owned source needs through one bulk revision read."""

        binding = self._binding(migration_run_id, actor=actor)
        data_version = self._data_versions.get(
            binding.data_version_id,
            actor=actor,
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
        selected_by_id = {item.recipe_id: item for item in binding.selected_revisions}
        requirements = []
        for recipe_id in self._recipe_order(binding, revisions):
            selection = selected_by_id[recipe_id]
            key = (selection.recipe_id, selection.recipe_revision)
            revision_read = revisions[key]
            envelope = revision_read.envelope
            if str(envelope.get("semantic_hash", "")) != selection.semantic_hash:
                raise RecipeError("The selected Recipe version has changed")
            recipe = revision_read.recipe
            definition = recipe_definition(envelope)
            requirements.append(
                FreshDataRecipeRequirement(
                    recipe_id=recipe.recipe_id,
                    recipe_revision=selection.recipe_revision,
                    display_name=recipe.display_name,
                    business_purpose=recipe.business_purpose,
                    inputs=fresh_input_requirements(definition),
                    parameters=fresh_parameter_requirements(
                        definition,
                        data_version.export_as_of,
                    ),
                )
            )
        return tuple(requirements)

    def run_value_plan(
        self,
        binding: TestRunSetupBinding,
        requirements: tuple[FreshDataRecipeRequirement, ...],
        *,
        actor: Actor,
    ) -> FreshDataRunValuePlan:
        """Merge identical Recipe requests so the data manager answers once."""

        current_binding = self._binding(binding.migration_run_id, actor=actor)
        if current_binding.content_hash != binding.content_hash:
            raise MigrationConflictError("Test setup changed; reload and retry")
        current = self._test_runs.get_parameter_values(binding.migration_run_id)
        assert_run_value_ownership(binding, current)
        return build_fresh_data_run_value_plan(requirements, current)

    def replace_run_values(
        self,
        binding: TestRunSetupBinding,
        supplied: Mapping[str, object],
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> TestRunParameterValues | None:
        """Validate and save only the answers declared by selected Recipes."""

        self._authorization.require(
            actor,
            Capability.MIGRATION_RUN_EDIT,
            project_id=binding.project_id,
        )
        current_binding = self._test_runs.get(binding.migration_run_id)
        if current_binding.content_hash != binding.content_hash:
            raise MigrationConflictError("Test setup changed; reload and retry")
        requirements = self.requirements(binding.migration_run_id, actor=actor)
        current = self._test_runs.get_parameter_values(binding.migration_run_id)
        assert_run_value_ownership(binding, current)
        current_revision = current.revision if current is not None else None
        if expected_revision != current_revision:
            raise MigrationConflictError("Run values changed; reload and retry")
        plan = build_fresh_data_run_value_plan(requirements, current)
        conflicts = tuple(item.conflict for item in plan.values if item.conflict)
        if conflicts:
            raise MigrationFoundationError(conflicts[0])
        editable = plan.editable_values
        expected_ids = {item.logical_parameter_id for item in editable}
        unknown = sorted(set(supplied) - expected_ids)
        if unknown:
            raise MigrationFoundationError(
                f"Run value {unknown[0]} is not requested by the selected Recipes"
            )
        if not editable:
            return current

        saved_values: list[RecipeRunParameterValue] = []
        for item in editable:
            definition = {
                "constraints": dict(item.constraints),
                "label": item.label,
                "logical_parameter_id": item.logical_parameter_id,
                "required": item.required,
                "type": item.value_type,
            }
            try:
                normalized = normalize_recipe_parameter_values(
                    (definition,),
                    {
                        item.logical_parameter_id: supplied.get(
                            item.logical_parameter_id,
                            "",
                        )
                    },
                )
            except RecipeParameterValueError as error:
                raise MigrationFoundationError(str(error)) from error
            if item.logical_parameter_id not in normalized:
                continue
            value = normalized[item.logical_parameter_id]
            for recipe_id in item.recipe_ids:
                saved_values.append(
                    RecipeRunParameterValue(
                        recipe_id=recipe_id,
                        logical_parameter_id=item.logical_parameter_id,
                        value=value,
                    )
                )
        ordered = tuple(
            sorted(
                saved_values,
                key=lambda item: (item.recipe_id, item.logical_parameter_id),
            )
        )
        if current is not None and current.values == ordered:
            return current
        data_version = self._data_versions.get(
            binding.data_version_id,
            actor=actor,
        )
        if current is not None and data_version.state is DataVersionState.FROZEN:
            raise MigrationConflictError(
                "Run details were accepted with this fresh data; start a new "
                "Test run to change them"
            )
        replacement = TestRunParameterValues(
            test_run_setup_id=binding.test_run_setup_id,
            project_id=binding.project_id,
            migration_run_id=binding.migration_run_id,
            revision=1 if current is None else current.revision + 1,
            values=ordered,
            updated_by=actor.identity,
            updated_at=utc_now(),
        )
        return self._test_runs.replace_parameter_values(
            replacement,
            expected_revision=expected_revision,
            actor=actor,
        )

    @staticmethod
    def match_plan(
        requirements: tuple[FreshDataRecipeRequirement, ...],
        catalogs: tuple[SourceFileCatalog, ...],
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> FreshDataMatchPlan:
        """Match Recipe logical inputs to bounded detected-table evidence."""

        return build_fresh_data_match_plan(
            requirements,
            catalogs,
            overrides=overrides,
        )

    def activation_parameter_values(
        self,
        binding: TestRunSetupBinding,
        export_as_of: object,
        *,
        actor: Actor,
    ) -> dict[str, dict[str, object]]:
        """Revalidate saved run values against exact Recipes before activation."""

        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self._recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        stored = self._test_runs.get_parameter_values(binding.migration_run_id)
        assert_run_value_ownership(binding, stored)
        stored_by_recipe = stored.by_recipe if stored is not None else {}
        selected_recipe_ids = {item.recipe_id for item in binding.selected_revisions}
        if set(stored_by_recipe) - selected_recipe_ids:
            raise MigrationConflictError(
                "Saved run values do not match the selected Recipes"
            )
        values: dict[str, dict[str, object]] = {}
        editable_declared = False
        for selection in binding.selected_revisions:
            revision = revisions[(selection.recipe_id, selection.recipe_revision)]
            definition = recipe_definition(revision.envelope)
            definitions = parameter_definitions(definition)
            declared = {
                str(item.get("logical_parameter_id", "")) for item in definitions
            }
            editable_declared = editable_declared or any(
                str(item.get("logical_parameter_id", "")) != EXPORT_AS_OF_PARAMETER_ID
                for item in definitions
            )
            recipe_values = dict(stored_by_recipe.get(selection.recipe_id, {}))
            if EXPORT_AS_OF_PARAMETER_ID in declared:
                recipe_values[EXPORT_AS_OF_PARAMETER_ID] = normalize_export_date(
                    export_as_of
                )
            try:
                values[selection.recipe_id] = normalize_recipe_parameter_values(
                    definitions,
                    recipe_values,
                )
            except RecipeParameterValueError as error:
                raise MigrationFoundationError(str(error)) from error
        if editable_declared and stored is None:
            raise MigrationFoundationError(
                "Confirm the Recipe details for this run on Fresh data"
            )
        return values

    def _binding(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> TestRunSetupBinding:
        binding = self._test_runs.get(migration_run_id)
        self._authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding

    @staticmethod
    def _recipe_order(binding, revisions) -> tuple[str, ...]:
        """Order source cards by dependency, then by Recipe business name."""

        recipe_ids = {item.recipe_id for item in binding.selected_revisions}
        selections = {item.recipe_id: item for item in binding.selected_revisions}
        incoming = {recipe_id: 0 for recipe_id in recipe_ids}
        downstream = {recipe_id: set() for recipe_id in recipe_ids}
        for dependency in binding.dependencies:
            if (
                dependency.after_recipe_id
                not in downstream[dependency.before_recipe_id]
            ):
                downstream[dependency.before_recipe_id].add(dependency.after_recipe_id)
                incoming[dependency.after_recipe_id] += 1

        def business_key(recipe_id):
            selection = selections[recipe_id]
            recipe = revisions[(selection.recipe_id, selection.recipe_revision)].recipe
            return (recipe.display_name.casefold(), recipe.recipe_id)

        ready = sorted(
            (recipe_id for recipe_id, count in incoming.items() if count == 0),
            key=business_key,
        )
        ordered = []
        while ready:
            recipe_id = ready.pop(0)
            ordered.append(recipe_id)
            for after_id in sorted(downstream[recipe_id], key=business_key):
                incoming[after_id] -= 1
                if incoming[after_id] == 0:
                    ready.append(after_id)
                    ready.sort(key=business_key)
        if len(ordered) != len(recipe_ids):
            ordered.extend(sorted(recipe_ids - set(ordered), key=business_key))
        return tuple(ordered)
