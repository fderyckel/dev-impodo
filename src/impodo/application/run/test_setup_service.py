"""Create and activate fresh Test evidence for selected Recipe versions."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_uuid,
)
from impodo.domain.run.contracts import RecipeDependency
from impodo.domain.run.test_setup import (
    TestRunParameterValues,
    TestRunSetupBinding,
    TestRunSetupBundle,
)

from .fresh_data_matching import (
    FreshDataMatchPlan,
    FreshDataRecipeRequirement,
)
from .fresh_data_setup import TestRunFreshDataUseCase
from .fresh_data_values import (
    FreshDataRunValuePlan,
)
from .odoo_requirements import (
    OdooCheckRequirementPlan,
    TestRunOdooRequirementsUseCase,
)
from .test_credential_workspace import TestRunCredentialWorkspaceUseCase
from .test_setup_start import TestRunSetupStartUseCase


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
        self._odoo_requirements = TestRunOdooRequirementsUseCase(
            test_runs=test_runs,
            recipes=recipes,
            authorization=authorization,
        )
        self._fresh_data = TestRunFreshDataUseCase(
            data_versions=data_versions,
            recipes=recipes,
            test_runs=test_runs,
            authorization=authorization,
        )
        self._start_setup = TestRunSetupStartUseCase(
            projects=projects,
            data_versions=data_versions,
            runs=runs,
            migration_workspaces=migration_workspaces,
            source_packages=source_packages,
            workspace_states=workspace_states,
            recipes=recipes,
            test_runs=test_runs,
            authorization=authorization,
        )

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
            parameter_values=self._fresh_data.activation_parameter_values(
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

        return self._fresh_data.requirements(migration_run_id, actor=actor)

    def fresh_data_run_value_plan(
        self,
        binding: TestRunSetupBinding,
        requirements: tuple[FreshDataRecipeRequirement, ...],
        *,
        actor: Actor,
    ) -> FreshDataRunValuePlan:
        """Merge identical Recipe requests so the data manager answers once."""

        return self._fresh_data.run_value_plan(
            binding,
            requirements,
            actor=actor,
        )

    def replace_fresh_data_run_values(
        self,
        binding: TestRunSetupBinding,
        supplied: Mapping[str, object],
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> TestRunParameterValues | None:
        """Validate and save only the answers declared by selected Recipes."""

        return self._fresh_data.replace_run_values(
            binding,
            supplied,
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
        return TestRunFreshDataUseCase.match_plan(
            requirements,
            catalogs,
            overrides=overrides,
        )

    def required_models_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[str, ...]:
        """Return the one combined Recipe-derived model scope for a run."""

        return self._odoo_requirements.required_models_for_workspace(
            workspace_id, actor=actor
        )

    def odoo_check_requirements_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooCheckRequirementPlan | None:
        """Read all selected revisions once and combine their Odoo needs."""

        return self._odoo_requirements.for_workspace(workspace_id, actor=actor)

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
