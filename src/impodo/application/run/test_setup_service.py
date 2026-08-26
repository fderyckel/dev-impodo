"""Create and activate fresh Test evidence for selected Recipe versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid5

from impodo.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.data_version.models import DataVersionPurpose, DataVersionState
from impodo.domain.recipe_parameters import (
    EXPORT_AS_OF_PARAMETER_ID,
    RecipeParameterValueError,
    normalize_recipe_parameter_values,
)
from impodo.domain.serialization import content_hash
from impodo.inspection import SourceFileCatalog
from impodo.migration_foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_uuid,
    utc_now,
)
from impodo.migration_run_planning import RecipeDependency, RecipeRevisionSelection
from impodo.domain.run.models import MigrationRunPurpose
from impodo.migration_test import (
    RecipeRunParameterValue,
    TestRunParameterValues,
    TestRunSetupBinding,
    TestRunSetupBundle,
)
from impodo.domain.recipe.models import RecipeError
from .test_credential_workspace import TestRunCredentialWorkspaceUseCase
from .test_setup_start import TestRunSetupStartUseCase
from .fresh_data_matching import (
    FreshDataInputRequirement,
    FreshDataMatchPlan,
    FreshDataMatchStatus,
    FreshDataParameterRequirement,
    FreshDataRecipeRequirement,
    build_fresh_data_match_plan,
)
from .fresh_data_values import (
    FreshDataRunValue,
    FreshDataRunValuePlan,
    assert_run_value_ownership,
    build_fresh_data_run_value_plan,
    fresh_input_requirements,
    fresh_parameter_requirements,
    normalize_export_date,
    parameter_definitions,
    recipe_definition,
)


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
    """Present one current Odoo value set required by the selected Recipes."""

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


class TestRunSetupService:
    """Own the guided Test setup before fresh Recipe work areas exist."""

    def __init__(
        self,
        *,
        projects,
        data_versions,
        runs,
        migration_workspaces,
        source_packages,
        workspace_states,
        recipes,
        test_runs,
        run_planning,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.runs = runs
        self.migration_workspaces = migration_workspaces
        self.source_packages = source_packages
        self.workspace_states = workspace_states
        self.recipes = recipes
        self.test_runs = test_runs
        self.run_planning = run_planning
        self.authorization = authorization
        self._credential_workspace = TestRunCredentialWorkspaceUseCase(
            test_runs=test_runs,
            workspace_states=workspace_states,
            authorization=authorization,
        )
        self._start_setup = TestRunSetupStartUseCase(self)

    def start_setup(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        label: str,
        export_as_of: str,
        operation_id: str,
        actor: Actor,
    ) -> TestRunSetupBundle:
        return self._start_setup.start(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            label=label,
            export_as_of=export_as_of,
            operation_id=operation_id,
            actor=actor,
        )

    def activate(
        self,
        project_id: str,
        migration_run_id: str,
        *,
        expected_workspace_revision: int,
        target_schema,
        target_reference_bundle,
        credential_generation: str,
        operation_id: str,
        actor: Actor,
    ):
        binding = self.get(migration_run_id, actor=actor)
        if binding.project_id != require_uuid(project_id, "project_id"):
            raise MigrationFoundationError("Test run does not belong to this Project")
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        return self.run_planning.activate_test_run(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            test_binding=binding,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            credential_generation=credential_generation,
            parameter_values=self._run_parameter_values(
                binding,
                data_version.export_as_of,
                actor=actor,
            ),
            operation_id=operation_id,
            actor=actor,
        )

    def get(self, migration_run_id: str, *, actor: Actor) -> TestRunSetupBinding:
        binding = self.test_runs.get(migration_run_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding

    def list(self, project_id: str, *, actor: Actor) -> tuple[TestRunSetupBinding, ...]:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor, Capability.PROJECT_VIEW, project_id=project_id
        )
        return self.test_runs.list_for_project(project_id)

    def fresh_data_requirements(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> tuple[FreshDataRecipeRequirement, ...]:
        """Return Recipe-owned source needs without reading per Recipe registry state."""

        binding = self.get(migration_run_id, actor=actor)
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self.recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        selected_by_id = {
            item.recipe_id: item for item in binding.selected_revisions
        }
        requirements = []
        for recipe_id in self._fresh_recipe_order(binding, revisions):
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

    def fresh_data_run_value_plan(
        self,
        binding: TestRunSetupBinding,
        requirements: tuple[FreshDataRecipeRequirement, ...],
        *,
        actor: Actor,
    ) -> FreshDataRunValuePlan:
        """Merge identical Recipe requests so the data manager answers once."""

        current_binding = self.get(binding.migration_run_id, actor=actor)
        if current_binding.content_hash != binding.content_hash:
            raise MigrationConflictError("Test setup changed; reload and retry")
        current = self.test_runs.get_parameter_values(binding.migration_run_id)
        assert_run_value_ownership(binding, current)
        return build_fresh_data_run_value_plan(requirements, current)

    def replace_fresh_data_run_values(
        self,
        binding: TestRunSetupBinding,
        supplied: Mapping[str, object],
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> TestRunParameterValues | None:
        """Validate and save only the answers declared by selected Recipes."""

        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_EDIT,
            project_id=binding.project_id,
        )
        current_binding = self.test_runs.get(binding.migration_run_id)
        if current_binding.content_hash != binding.content_hash:
            raise MigrationConflictError("Test setup changed; reload and retry")
        requirements = self.fresh_data_requirements(
            binding.migration_run_id,
            actor=actor,
        )
        current = self.test_runs.get_parameter_values(binding.migration_run_id)
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
        data_version = self.data_versions.get(
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
        return self.test_runs.replace_parameter_values(
            replacement,
            expected_revision=expected_revision,
            actor=actor,
        )

    @staticmethod
    def fresh_data_match_plan(
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

    @staticmethod
    def _fresh_recipe_order(binding, revisions):
        """Order source cards by dependency, then by the Recipe's business name."""

        recipe_ids = {item.recipe_id for item in binding.selected_revisions}
        selections = {
            item.recipe_id: item for item in binding.selected_revisions
        }
        incoming = {recipe_id: 0 for recipe_id in recipe_ids}
        downstream = {recipe_id: set() for recipe_id in recipe_ids}
        for dependency in binding.dependencies:
            if dependency.after_recipe_id not in downstream[dependency.before_recipe_id]:
                downstream[dependency.before_recipe_id].add(
                    dependency.after_recipe_id
                )
                incoming[dependency.after_recipe_id] += 1

        def business_key(recipe_id):
            selection = selections[recipe_id]
            recipe = revisions[
                (selection.recipe_id, selection.recipe_revision)
            ].recipe
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

    def required_models_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[str, ...]:
        """Return the one combined Recipe-derived model scope for a run."""

        plan = self.odoo_check_requirements_for_workspace(
            workspace_id,
            actor=actor,
        )
        return plan.model_names if plan is not None else ()

    def odoo_check_requirements_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooCheckRequirementPlan | None:
        """Read all selected revisions once and combine their Odoo needs."""

        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None:
            return None
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self.recipes.read_revisions(
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
            if str(revision.envelope.get("semantic_hash", "")) != selection.semantic_hash:
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
                    relationship_field = str(
                        path.get("relationship_field", "")
                    ).strip()
                    relationship_type = str(
                        path.get("relationship_type", "")
                    ).strip()
                    if not (
                        model_name
                        and key_fields
                        and parent_model
                        and relationship_field
                        and relationship_type
                    ):
                        raise RecipeError(
                            "A selected Recipe contains an incomplete Odoo relationship"
                        )
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

    def setup_binding_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> TestRunSetupBinding | None:
        """Return the owning setup only when this is its shared workspace."""

        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None or binding.setup_workspace_id != workspace_id:
            return None
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding

    def credential_workspace(self, workspace_id: str, *, actor: Actor):
        """Return the shared Test setup workspace that owns target credentials."""
        return self._credential_workspace.workspace(workspace_id, actor=actor)

    def credential_workspace_id(self, workspace_id: str, *, actor: Actor) -> str:
        """Return the credential owner without opening another workspace store."""
        return self._credential_workspace.workspace_id(workspace_id, actor=actor)

    def _selections(self, project_id, values, *, actor):
        normalized = tuple(values)
        revisions = self.recipes.read_revisions(
            project_id,
            normalized,
            actor=actor,
        )
        selections = []
        for recipe_id, version in normalized:
            revision_read = revisions[(recipe_id, version)]
            recipe = revision_read.recipe
            if recipe.project_id != project_id:
                raise MigrationFoundationError("Recipe belongs to another Project")
            envelope = revision_read.envelope
            selections.append(
                RecipeRevisionSelection(
                    recipe_id=recipe_id,
                    recipe_revision=version,
                    semantic_hash=str(envelope["semantic_hash"]),
                )
            )
        return tuple(selections)

    def _run_parameter_values(self, binding, export_as_of, *, actor):
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self.recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        stored = self.test_runs.get_parameter_values(binding.migration_run_id)
        assert_run_value_ownership(binding, stored)
        stored_by_recipe = stored.by_recipe if stored is not None else {}
        selected_recipe_ids = {
            item.recipe_id for item in binding.selected_revisions
        }
        if set(stored_by_recipe) - selected_recipe_ids:
            raise MigrationConflictError(
                "Saved run values do not match the selected Recipes"
            )
        values = {}
        editable_declared = False
        for selection in binding.selected_revisions:
            definition = recipe_definition(
                revisions[(selection.recipe_id, selection.recipe_revision)].envelope
            )
            definitions = parameter_definitions(definition)
            declared = {
                str(item.get("logical_parameter_id", "")) for item in definitions
            }
            editable_declared = editable_declared or any(
                str(item.get("logical_parameter_id", ""))
                != EXPORT_AS_OF_PARAMETER_ID
                for item in definitions
            )
            recipe_values = dict(stored_by_recipe.get(selection.recipe_id, {}))
            if EXPORT_AS_OF_PARAMETER_ID in declared:
                recipe_values[EXPORT_AS_OF_PARAMETER_ID] = (
                    normalize_export_date(export_as_of)
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

    def _committed_setup(
        self,
        project_id,
        *,
        operation_id,
        label,
        export_as_of,
        selections,
        dependencies,
        actor,
    ):
        bind_operation = self._child_operation(operation_id, "bind-test-setup")
        try:
            intent = self.test_runs.foundation.get_operation_intent(bind_operation)
        except MigrationNotFoundError:
            return None
        if (
            intent.kind is not MigrationOperationKind.TEST_RUN_SETUP
            or intent.project_id != project_id
            or intent.actor.issuer != actor.identity.issuer
            or intent.actor.subject_id != actor.identity.subject_id
        ):
            raise MigrationConflictError(
                "Operation identity was already used with different meaning"
            )
        if intent.state is not MigrationOperationState.COMMITTED:
            return None
        binding = self.test_runs.get(intent.owner_id)
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        if (
            binding.selected_revisions != selections
            or binding.dependencies != tuple(sorted(dependencies))
            or data_version.label != label
            or data_version.export_as_of != export_as_of.strip()
        ):
            raise MigrationConflictError(
                "Operation identity was already used for another Test setup"
            )
        return self._bundle(binding, actor=actor)

    def _bundle(self, binding, *, actor):
        return TestRunSetupBundle(
            data_version=self.data_versions.get(binding.data_version_id, actor=actor),
            run=self.runs.get(binding.migration_run_id, actor=actor),
            setup_workspace=self.migration_workspaces.get(
                binding.setup_workspace_id,
                actor=actor,
            ),
            binding=binding,
        )

    def _data_version(
        self,
        project_id,
        *,
        expected_workspace_revision,
        parent_data_version_id,
        label,
        export_as_of,
        operation_id,
        actor,
    ):
        try:
            intent = self.data_versions.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.data_versions.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                purpose=DataVersionPurpose.TEST,
                label=label,
                export_as_of=export_as_of,
                parent_data_version_id=parent_data_version_id,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.data_versions.repository.get_data_version(intent.owner_id)
        return self.data_versions.repository.resume_data_version_creation(
            operation_id,
            actor=actor,
        )

    def _run(
        self,
        project_id,
        *,
        expected_workspace_revision,
        data_version_id,
        label,
        operation_id,
        actor,
    ):
        try:
            intent = self.runs.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.runs.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                data_version_id=data_version_id,
                purpose=MigrationRunPurpose.TEST,
                label=label,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.runs.repository.get_migration_run(intent.owner_id)
        return self.runs.repository.resume_migration_run_creation(
            operation_id,
            actor=actor,
        )

    def _workspace(
        self,
        project_id,
        *,
        expected_workspace_revision,
        data_version_id,
        migration_run_id,
        label,
        operation_id,
        actor,
    ):
        try:
            intent = self.migration_workspaces.repository.get_operation_intent(
                operation_id
            )
        except MigrationNotFoundError:
            return self.migration_workspaces.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                data_version_id=data_version_id,
                migration_run_id=migration_run_id,
                display_name=label,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.migration_workspaces.repository.get_migration_workspace(
                intent.owner_id
            )
        return self.migration_workspaces.repository.resume_migration_workspace_creation(
            operation_id,
            actor=actor,
        )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))
