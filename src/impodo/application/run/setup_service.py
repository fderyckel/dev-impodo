"""Coordinate the shared Recipe-aware setup journey for Test and Production."""

from dataclasses import dataclass

from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.run.contracts import RecipeRevisionSelection
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.run.production import ProductionRunBinding
from impodo.domain.shared.access import Actor, Capability
from .fresh_data_matching import build_fresh_data_match_plan
from .odoo_requirements import RunOdooRequirementsUseCase


@dataclass(frozen=True, slots=True)
class RunSetupSelection:
    """Project only the exact Recipe selection needed for the shared Odoo query."""

    project_id: str
    migration_run_id: str
    setup_workspace_id: str
    selected_revisions: tuple[RecipeRevisionSelection, ...]


class RunSetupService:
    """Share setup decisions while retaining separate aggregate persistence."""

    def __init__(self, *, test_runs, production_runs, runs, recipes, authorization):
        self.test_runs = test_runs
        self.production_runs = production_runs
        self.runs = runs
        self.authorization = authorization
        self.odoo = RunOdooRequirementsUseCase(setups=self, recipes=recipes, authorization=authorization)

    def get(self, migration_run_id: str, *, actor: Actor):
        run = self.runs.get(migration_run_id, actor=actor)
        if run.purpose is MigrationRunPurpose.TEST:
            return self.test_runs.get(migration_run_id, actor=actor)
        if run.purpose is MigrationRunPurpose.PRODUCTION:
            return self.production_runs.production_runs.get(migration_run_id)
        raise MigrationFoundationError("This run does not have a Recipe setup")

    def for_workspace(self, workspace_id: str):
        """Resolve setup ownership using bounded registry reads, without child stores."""

        binding = self.test_runs.test_runs.for_workspace(workspace_id)
        if binding is not None:
            return RunSetupSelection(binding.project_id, binding.migration_run_id, binding.setup_workspace_id, binding.selected_revisions)
        binding = self.production_runs.production_runs.for_workspace(workspace_id)
        if binding is None or binding.setup_workspace_id != workspace_id:
            return None
        plan = self._production_plan(binding)
        return RunSetupSelection(binding.project_id, binding.migration_run_id, binding.setup_workspace_id, plan.selected_revisions)

    def setup_binding_for_workspace(self, workspace_id: str, *, actor: Actor):
        selection = self.for_workspace(workspace_id)
        if selection is None or selection.setup_workspace_id != workspace_id:
            return None
        return self.get(selection.migration_run_id, actor=actor)

    def fresh_data_details(self, binding, *, actor: Actor):
        """Read the selected Recipes once for both source prompts and saved answers."""

        if isinstance(binding, ProductionRunBinding):
            review = self._production_values(binding, actor=actor)
            return review.requirements, review.values
        requirements = self.test_runs.fresh_data_requirements(binding.migration_run_id, actor=actor)
        return requirements, self.test_runs.fresh_data_run_value_plan(binding, requirements, actor=actor)

    def replace_values(self, binding, supplied, *, supplied_controls=None, expected_revision=None, actor: Actor):
        if isinstance(binding, ProductionRunBinding):
            self.authorization.require(actor, Capability.PRODUCTION_RUN_ACTIVATE, project_id=binding.project_id)
            self._assert_current_production_selection(binding)
            review = self._production_values(binding, actor=actor)
            return self.production_runs.values.save(binding, review, supplied, supplied_controls or {},
                expected_revision=expected_revision, actor=actor)
        return self.test_runs.replace_fresh_data_run_values(binding, supplied, supplied_controls=supplied_controls,
            expected_revision=expected_revision, actor=actor)

    @staticmethod
    def match_plan(requirements, catalogs, *, overrides=None):
        return build_fresh_data_match_plan(requirements, catalogs, overrides=overrides)

    def odoo_check_requirements_for_workspace(self, workspace_id: str, *, actor: Actor):
        return self.odoo.for_workspace(workspace_id, actor=actor)

    def required_models_for_workspace(self, workspace_id: str, *, actor: Actor):
        return self.odoo.required_models_for_workspace(workspace_id, actor=actor)

    def _production_plan(self, binding):
        plan = self.production_runs.cutover_plans.get_revision(binding.cutover_plan_id, binding.cutover_plan_revision)
        if plan.content_hash != binding.plan_content_hash:
            raise MigrationFoundationError("Production setup no longer matches its qualified plan")
        return plan

    def _production_values(self, binding, *, actor: Actor):
        return self.production_runs.values.review(binding, self._production_plan(binding),
            self.production_runs.data_versions.get(binding.data_version_id, actor=actor), actor=actor)

    def _assert_current_production_selection(self, binding):
        selection = self.production_runs.cutover_plans.current_selection(binding.project_id)
        plan = self.production_runs.cutover_plans.get_plan(binding.cutover_plan_id)
        if (selection is None or selection.cutover_selection_id != binding.cutover_selection_id
                or plan.current_revision != binding.cutover_plan_revision):
            raise MigrationFoundationError("The selected Cutover plan changed; start a new Production setup")
