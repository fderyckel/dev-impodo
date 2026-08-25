"""Create and activate fresh Test evidence for selected Recipe versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid5

from ..access import Actor, AuthorizationPolicy, Capability
from ..data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageOrigin,
    SourcePackageState,
)
from ..data_versions import DataVersionPurpose, DataVersionState
from ..domain.serialization import content_hash
from ..migration_foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ..migration_run_planning import RecipeDependency, RecipeRevisionSelection
from ..migration_runs import MigrationRunPurpose
from ..migration_test import (
    TestRunSetupBinding,
    TestRunSetupBundle,
    TestRunSetupState,
)
from ..workspace_state import SourceMode, WorkspaceStateNotFoundError


@dataclass(frozen=True, slots=True)
class FreshDataInputRequirement:
    """Describe one logical source table that a Recipe expects."""

    logical_dataset_id: str
    label: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FreshDataParameterRequirement:
    """Describe one value supplied by this run rather than by source rows."""

    logical_parameter_id: str
    label: str
    value_type: str
    required: bool
    supplied_value: str | None


@dataclass(frozen=True, slots=True)
class FreshDataRecipeRequirement:
    """Present the source contract of one exact selected Recipe revision."""

    recipe_id: str
    recipe_revision: int
    display_name: str
    business_purpose: str
    inputs: tuple[FreshDataInputRequirement, ...]
    parameters: tuple[FreshDataParameterRequirement, ...]


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
        """Create one draft Test delivery and its shared setup workspace."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        clean_export_as_of = required_text(
            export_as_of,
            "export_as_of",
            maximum=100,
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        selections = self._selections(project_id, recipe_revisions, actor=actor)
        replay = self._committed_setup(
            project_id,
            operation_id=operation_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            selections=selections,
            dependencies=dependencies,
            actor=actor,
        )
        if replay is not None:
            return replay

        authoring_versions = tuple(
            item
            for item in self.data_versions.list(project_id, actor=actor)
            if item.purpose is DataVersionPurpose.AUTHORING
            and item.state is DataVersionState.FROZEN
        )
        if not authoring_versions:
            raise MigrationFoundationError(
                "Save the Recipe from an accepted Authoring data version first"
            )
        parent = max(authoring_versions, key=lambda item: item.version_number)
        parent_package = self.source_packages.repository.get_source_package(
            parent.data_version_id
        )
        if (
            parent_package is None
            or parent_package.origin is not SourcePackageOrigin.FILE
        ):
            raise MigrationFoundationError(
                "Testing with a newer delivery currently requires a file-source Recipe"
            )

        data_version = self._data_version(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            parent_data_version_id=parent.data_version_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            operation_id=self._child_operation(operation_id, "test-data"),
            actor=actor,
        )
        package = self.source_packages.repository.get_source_package(
            data_version.data_version_id
        )
        if package is None:
            self.source_packages.replace_draft(
                DataVersionSourcePackage(
                    data_version_id=data_version.data_version_id,
                    project_id=project_id,
                    revision=1,
                    origin=SourcePackageOrigin.FILE,
                    state=SourcePackageState.DRAFT,
                    files=(),
                    catalogs=(),
                    configurations=(),
                    datasets=(),
                    updated_at=datetime.now(UTC),
                ),
                actor=actor,
                expected_package_revision=None,
            )
        project = self.projects.get(project_id, actor=actor)
        run = self._run(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            label=clean_label,
            operation_id=self._child_operation(operation_id, "test-run"),
            actor=actor,
        )
        project = self.projects.get(project_id, actor=actor)
        setup_workspace = self._workspace(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            label=f"{clean_label} data and Odoo target setup",
            operation_id=self._child_operation(operation_id, "test-setup-workspace"),
            actor=actor,
        )
        try:
            self.workspace_states.repository.get(setup_workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            project = self.projects.get(project_id, actor=actor)
            self.workspace_states.provision_migration_workspace(
                setup_workspace.workspace_id,
                actor=actor,
                name=setup_workspace.display_name,
                source_system=project.source_system_identity,
                source_mode=SourceMode.FILE,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        binding = TestRunSetupBinding(
            test_run_setup_id=self._child_operation(operation_id, "test-binding"),
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=data_version.data_version_id,
            setup_workspace_id=setup_workspace.workspace_id,
            selected_revisions=selections,
            dependencies=dependencies,
            state=TestRunSetupState.SETUP,
            target_binding_id=None,
            created_at=utc_now(),
        )
        project = self.projects.get(project_id, actor=actor)
        stored = self.test_runs.bind_setup(
            binding,
            expected_workspace_revision=project.optimistic_revision,
            operation_id=self._child_operation(operation_id, "bind-test-setup"),
            request_hash=content_hash(
                {
                    "binding": binding.to_dict(),
                    "export_as_of": clean_export_as_of,
                    "label": clean_label,
                }
            ),
            actor=actor,
        )
        return self._bundle(stored, actor=actor)

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
            definition = self._recipe_definition(envelope)
            requirements.append(
                FreshDataRecipeRequirement(
                    recipe_id=recipe.recipe_id,
                    recipe_revision=selection.recipe_revision,
                    display_name=recipe.display_name,
                    business_purpose=recipe.business_purpose,
                    inputs=self._fresh_inputs(definition),
                    parameters=self._fresh_parameters(
                        definition,
                        data_version.export_as_of,
                    ),
                )
            )
        return tuple(requirements)

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
        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None:
            return ()
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
        models = set()
        for selection in binding.selected_revisions:
            definition = self._recipe_definition(
                revisions[(selection.recipe_id, selection.recipe_revision)].envelope
            )
            contract = dict(definition.get("odoo_target_contract", {}))
            for model in contract.get("models", ()):  # type: ignore[union-attr]
                model_name = str(dict(model).get("model", "")).strip()
                if model_name:
                    models.add(model_name)
        return tuple(sorted(models))

    def credential_workspace(self, workspace_id: str, *, actor: Actor):
        """Return the shared Test setup workspace that owns target credentials."""

        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None:
            return self.workspace_states.repository.get(workspace_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return self.workspace_states.repository.get(binding.setup_workspace_id)

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
        values = {}
        for selection in binding.selected_revisions:
            definition = self._recipe_definition(
                revisions[(selection.recipe_id, selection.recipe_revision)].envelope
            )
            declared = {
                str(item.get("logical_parameter_id", ""))
                for item in self._parameter_definitions(definition)
            }
            recipe_values = {}
            if "parameter:export_as_of_date" in declared:
                recipe_values["parameter:export_as_of_date"] = (
                    self._export_date(export_as_of)
                )
            values[selection.recipe_id] = recipe_values
        return values

    @staticmethod
    def _recipe_definition(envelope) -> Mapping[str, object]:
        definition = envelope.get("recipe")
        if not isinstance(definition, Mapping):
            raise RecipeError("Stored Recipe source requirements are invalid")
        return definition

    @staticmethod
    def _parameter_definitions(definition) -> tuple[Mapping[str, object], ...]:
        payload = definition.get("parameter_definitions", {})
        if not isinstance(payload, Mapping):
            raise RecipeError("Stored Recipe run values are invalid")
        parameters = payload.get("parameters", ())
        if not isinstance(parameters, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in parameters
        ):
            raise RecipeError("Stored Recipe run values are invalid")
        return tuple(parameters)

    @classmethod
    def _fresh_parameters(cls, definition, export_as_of):
        return tuple(
            FreshDataParameterRequirement(
                logical_parameter_id=str(item.get("logical_parameter_id", "")),
                label=str(item.get("label", "Run value")),
                value_type=str(item.get("type", "string")),
                required=bool(item.get("required", False)),
                supplied_value=(
                    cls._export_date(export_as_of)
                    if item.get("logical_parameter_id")
                    == "parameter:export_as_of_date"
                    else None
                ),
            )
            for item in cls._parameter_definitions(definition)
        )

    @staticmethod
    def _export_date(export_as_of):
        candidate = str(export_as_of).strip()[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError as error:
            raise MigrationFoundationError(
                "The Test delivery cutoff must start with a year-month-day date"
            ) from error

    @staticmethod
    def _fresh_inputs(definition) -> tuple[FreshDataInputRequirement, ...]:
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
