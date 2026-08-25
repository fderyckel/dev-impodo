"""Create and activate fresh Test evidence for selected Recipe versions."""

from __future__ import annotations

from datetime import UTC, datetime
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
        return self.run_planning.activate_test_run(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            test_binding=binding,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            credential_generation=credential_generation,
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
        models = set()
        for selection in binding.selected_revisions:
            envelope = self.recipes.read_revision(
                selection.recipe_id,
                selection.recipe_revision,
                actor=actor,
            )
            definition = dict(envelope["recipe"])
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
        selections = []
        for recipe_id, version in values:
            recipe = self.recipes.get(recipe_id, actor=actor)
            if recipe.project_id != project_id:
                raise MigrationFoundationError("Recipe belongs to another Project")
            envelope = self.recipes.read_revision(recipe_id, version, actor=actor)
            selections.append(
                RecipeRevisionSelection(
                    recipe_id=recipe_id,
                    recipe_revision=version,
                    semantic_hash=str(envelope["semantic_hash"]),
                )
            )
        return tuple(selections)

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
