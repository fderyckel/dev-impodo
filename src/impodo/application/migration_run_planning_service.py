"""Plan Project-owned Test and Production runs for several Recipes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4, uuid5

from ..access import Actor, AuthorizationPolicy, Capability
from ..data_version_sources import (
    DataVersionDatasetView,
    DataVersionSourcePackage,
    SourcePackageOrigin,
    WorkspaceSourceProjectionService,
)
from ..data_versions import DataVersionPurpose, DataVersionService, DataVersionState
from ..domain.coverage import ReferenceBundle
from ..domain.mapping.contracts import ScalarValueSource, TargetFieldHandling
from ..domain.serialization import content_hash
from ..migration_cutover import (
    PROJECT_SHARED_CONTROL_IDS,
    CutoverPlanRevision,
    CutoverWriteOwnership,
)
from ..migration_foundation import (
    FaultInjector,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ..migration_production import (
    ProductionRunBinding,
    ProductionRunBindingState,
    ProductionRunError,
    activation_evidence_hash,
)
from ..migration_projects import MigrationProjectService
from ..migration_run_planning import (
    IntegratedRunBundle,
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    MigrationRunPlanningError,
    MigrationRunReferenceBundle,
    MigrationRunRequirementPlan,
    MigrationRunTargetSchema,
    OdooModelRequirement,
    PlannedRecipeApplication,
    RecipeApplicationStatus,
    RecipeDependency,
    RecipeRevisionSelection,
    ReferenceRequirement,
    RunRecipeApplication,
    RunTargetBinding,
)
from ..migration_runs import MigrationRun, MigrationRunPurpose, MigrationRunState
from ..migration_test import TestRunSetupBinding, TestRunSetupState
from ..migration_workspaces import (
    MigrationWorkspace,
    MigrationWorkspaceState,
)
from ..models import OdooWriteIdentity
from ..recipes import Recipe, RecipeService
from ..workspace_contracts import OdooSchemaCatalog
from ..workspace_state import (
    SourceMode,
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
)
from .recipe_application_service import (
    RecipeApplicationAssessment,
    RecipeApplicationService,
)


@dataclass(frozen=True, slots=True)
class ReviewedRecipeApplication:
    recipe: Recipe
    selection: RecipeRevisionSelection
    definition: Mapping[str, object]
    requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    write_claims: tuple[tuple[str, str], ...]
    assessment: RecipeApplicationAssessment


@dataclass(frozen=True, slots=True)
class IntegratedRunReview:
    """Show planning blockers before any run or workspace is created."""

    project_id: str
    data_version_id: str
    applications: tuple[ReviewedRecipeApplication, ...]
    dependencies: tuple[RecipeDependency, ...]
    model_requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    application_order: tuple[str, ...]
    planning_issues: tuple[MigrationRunPlanIssue, ...]

    @property
    def can_start(self) -> bool:
        return not any(item.blocks for item in self.planning_issues)


class MigrationRunPlanningService:
    """Own integrated run validation, isolation, and compiler orchestration."""

    def __init__(
        self,
        *,
        projects: MigrationProjectService,
        data_versions: DataVersionService,
        recipes: RecipeService,
        repository,
        source_packages,
        source_projections: WorkspaceSourceProjectionService,
        workspace_states: WorkspaceStateService,
        compiler: RecipeApplicationService,
        cutover_plans,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.recipes = recipes
        self.repository = repository
        self.source_packages = source_packages
        self.source_projections = source_projections
        self.workspace_states = workspace_states
        self.compiler = compiler
        self.cutover_plans = cutover_plans
        self.authorization = authorization

    def review_test_run(
        self,
        project_id: str,
        *,
        data_version_id: str,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        actor: Actor,
    ) -> IntegratedRunReview:
        """Validate dependency and write ownership before provisioning."""

        return self._review_run(
            project_id,
            data_version_id=data_version_id,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            purpose=DataVersionPurpose.TEST,
            required_target_workspace_id=None,
            actor=actor,
        )

    def review_production_run(
        self,
        project_id: str,
        *,
        production_binding: ProductionRunBinding,
        plan: CutoverPlanRevision,
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        test_connection_target_hash: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        shared_control_values: Mapping[str, bool],
        actor: Actor,
    ) -> IntegratedRunReview:
        """Review fresh rollout evidence against exact qualified plan meaning."""

        if (
            production_binding.project_id != project_id
            or production_binding.cutover_plan_id != plan.cutover_plan_id
            or production_binding.cutover_plan_revision != plan.version
            or production_binding.plan_content_hash != plan.content_hash
        ):
            raise ProductionRunError(
                "Production setup does not match the selected CutoverPlan"
            )
        review = self._review_run(
            project_id,
            data_version_id=production_binding.data_version_id,
            recipe_revisions=tuple(
                (item.recipe_id, item.recipe_revision)
                for item in plan.selected_revisions
            ),
            dependencies=plan.dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            purpose=DataVersionPurpose.PRODUCTION,
            required_target_workspace_id=production_binding.setup_workspace_id,
            actor=actor,
        )
        issues = list(review.planning_issues)
        if target_schema.connection_target_hash == test_connection_target_hash:
            issues.append(
                self._block(
                    "PRODUCTION_TARGET_NOT_INDEPENDENT",
                    "Production uses the same Odoo target as Integrated Test.",
                    "Capture the compatible Odoo 19 Production database instead.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        try:
            odoo_major = int(str(target_schema.odoo_version).split(".", 1)[0])
        except ValueError:
            odoo_major = -1
        if odoo_major != 19 or target_schema.origin.value != "LIVE_API":
            issues.append(
                self._block(
                    "PRODUCTION_TARGET_EVIDENCE_UNSUPPORTED",
                    "Production target evidence is not a current live Odoo 19 capture.",
                    "Capture the Production Odoo 19 fields and supporting lists again.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        if self._semantic_requirement_hash(review) != plan.requirement_plan_hash:
            issues.append(
                self._block(
                    "PRODUCTION_PLAN_MEANING_CHANGED",
                    "The current Recipe requirements no longer match the qualified plan.",
                    "Publish and qualify a new CutoverPlan revision.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        current_ownership = tuple(
            sorted(
                CutoverWriteOwnership(
                    recipe_id=item.selection.recipe_id,
                    model=model,
                    field=field,
                )
                for item in review.applications
                for model, field in item.write_claims
            )
        )
        if current_ownership != plan.write_ownership:
            issues.append(
                self._block(
                    "PRODUCTION_WRITE_OWNERSHIP_CHANGED",
                    "Current Recipe write ownership differs from the qualified plan.",
                    "Publish and qualify the corrected CutoverPlan before Production.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        if set(shared_control_values) != set(PROJECT_SHARED_CONTROL_IDS):
            issues.append(
                self._block(
                    "PRODUCTION_SHARED_CONTROLS_INCOMPLETE",
                    "The Production run does not contain every Project control.",
                    "Review package completeness and integrated reconciliation controls.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        elif not shared_control_values[
            "control:project.package_completeness"
        ]:
            issues.append(
                self._block(
                    "PRODUCTION_PACKAGE_INCOMPLETE",
                    "The latest Production delivery is not confirmed complete.",
                    "Accept the complete Production data version before activation.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        if shared_control_values.get(
            "control:project.integrated_reconciliation"
        ):
            issues.append(
                self._block(
                    "PRODUCTION_RECONCILIATION_PREMATURE",
                    "Production reconciliation was marked complete before execution.",
                    "Leave it pending until every application is verified.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        return replace(
            review,
            planning_issues=tuple(
                sorted(
                    {
                        content_hash(item.to_dict()): item
                        for item in issues
                    }.values(),
                    key=lambda item: (item.code, item.recipe_ids),
                )
            ),
        )

    def _review_run(
        self,
        project_id: str,
        *,
        data_version_id: str,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        purpose: DataVersionPurpose,
        required_target_workspace_id: str | None,
        actor: Actor,
    ) -> IntegratedRunReview:
        """Validate one Test or Production plan without creating workspaces."""

        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        self.authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=project_id,
        )
        project = self.projects.get(project_id, actor=actor)
        target_workspace = self.repository.foundation.get_migration_workspace(
            require_uuid(target_schema.workspace_id, "target evidence workspace_id")
        )
        target_data_version = self.data_versions.repository.get_data_version(
            target_workspace.data_version_id
        )
        if (
            target_workspace.project_id != project_id
            or target_workspace.recipe_application_id is not None
            or (
                purpose is DataVersionPurpose.TEST
                and target_data_version.purpose is DataVersionPurpose.PRODUCTION
            )
            or (
                required_target_workspace_id is not None
                and target_workspace.workspace_id != required_target_workspace_id
            )
        ):
            raise MigrationRunPlanningError(
                "Choose reviewed Odoo evidence from this run's setup workspace"
            )
        if (
            target_reference_bundle is not None
            and target_reference_bundle.workspace_id != target_workspace.workspace_id
        ):
            raise MigrationRunPlanningError(
                "The supporting lists do not match the reviewed Odoo workspace"
            )
        data_version = self.data_versions.get(data_version_id, actor=actor)
        if (
            data_version.project_id != project.project_id
            or data_version.purpose is not purpose
            or data_version.state is not DataVersionState.FROZEN
        ):
            raise MigrationRunPlanningError(
                f"Choose one accepted {purpose.value.title()} DataVersion from this Project"
            )
        package = self.source_packages.repository.get_source_package(data_version_id)
        if package is None or package.content_hash != data_version.source_package_hash:
            raise MigrationRunPlanningError(
                f"The {purpose.value.title()} DataVersion source evidence is "
                "missing or inconsistent"
            )
        normalized = tuple(
            sorted(
                (
                    require_uuid(recipe_id, "recipe_id"),
                    require_revision(version, "recipe_revision"),
                )
                for recipe_id, version in recipe_revisions
            )
        )
        if not normalized or len({item[0] for item in normalized}) != len(normalized):
            raise MigrationRunPlanningError(
                f"Select one revision from each Recipe used by this {purpose.value.title()} run"
            )
        source_selection = self._package_selection(package)
        supplied_parameters = parameter_values or {}
        supplied_controls = control_values or {}
        applications = []
        for recipe_id, version in normalized:
            recipe = self.recipes.get(recipe_id, actor=actor)
            if recipe.project_id != project_id:
                raise MigrationRunPlanningError(
                    "Every selected Recipe must belong to this Project"
                )
            envelope = self.recipes.read_revision(recipe_id, version, actor=actor)
            semantic_hash = str(envelope["semantic_hash"])
            definition = dict(envelope["recipe"])
            selection = RecipeRevisionSelection(
                recipe_id=recipe_id,
                recipe_revision=version,
                semantic_hash=semantic_hash,
            )
            applications.append(
                ReviewedRecipeApplication(
                    recipe=recipe,
                    selection=selection,
                    definition=definition,
                    requirements=self.compiler.requirements(definition),
                    reference_requirements=(
                        self.compiler.reference_requirements(definition)
                    ),
                    write_claims=self.compiler.write_claims(definition),
                    assessment=self.compiler.assess(
                        recipe_id=recipe_id,
                        definition=definition,
                        source_selection=source_selection,
                        target_schema=target_schema,
                        reference_bundle=target_reference_bundle,
                        parameter_values=supplied_parameters.get(recipe_id, {}),
                        control_values=supplied_controls.get(recipe_id, {}),
                    ),
                )
            )
        selected_ids = {item.selection.recipe_id for item in applications}
        planning_issues = []
        application_order = self._application_order(
            selected_ids,
            dependencies,
            planning_issues,
        )
        self._write_collision_issues(applications, planning_issues)
        requirements = self._union_requirements(applications)
        reference_requirements = self._union_reference_requirements(
            applications,
            planning_issues,
        )
        if target_schema.connection_target_hash.strip() == "":
            planning_issues.append(
                self._block(
                    "RUN_TARGET_IDENTITY_MISSING",
                    "The selected Odoo evidence has no exact target identity.",
                    "Capture current Odoo 19 evidence before starting the "
                    f"{purpose.value.title()} run.",
                    tuple(selected_ids),
                )
            )
        return IntegratedRunReview(
            project_id=project_id,
            data_version_id=data_version_id,
            applications=tuple(applications),
            dependencies=tuple(
                sorted(
                    dependencies,
                    key=lambda item: (
                        item.before_recipe_id,
                        item.after_recipe_id,
                    ),
                )
            ),
            model_requirements=requirements,
            reference_requirements=reference_requirements,
            application_order=application_order,
            planning_issues=tuple(
                sorted(
                    planning_issues,
                    key=lambda item: (item.code, item.recipe_ids),
                )
            ),
        )

    def start_test_run(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        data_version_id: str,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        credential_generation: str,
        label: str,
        operation_id: str,
        actor: Actor,
        parameter_values: Mapping[str, Mapping[str, object]] | None = None,
        control_values: Mapping[str, Mapping[str, str]] | None = None,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Provision and materialize one restart-safe multi-Recipe Test run."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        credential_generation = required_text(
            credential_generation,
            "credential_generation",
            maximum=300,
        )
        if credential_generation != target_schema.read_credential_binding_hash:
            raise MigrationRunPlanningError(
                "The reviewed Odoo evidence belongs to another read credential generation"
            )
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_CREATE,
            project_id=project_id,
        )
        review = self.review_test_run(
            project_id,
            data_version_id=data_version_id,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise MigrationRunPlanningError(
                f"{first.message} {first.recovery_action}"
            )
        now = utc_now()
        run_id = self._child_operation(operation_id, "migration-run")
        target_binding_id = self._child_operation(operation_id, "target-binding")
        required_reference_names = {
            item.name for item in review.reference_requirements
        }
        captured_reference_datasets = tuple(
            item
            for item in (
                target_reference_bundle.datasets
                if target_reference_bundle is not None
                else ()
            )
            if item.name in required_reference_names
        )
        run_reference_bundle = (
            MigrationRunReferenceBundle.capture(
                run_id,
                target_reference_bundle,
                captured_reference_datasets,
            )
            if captured_reference_datasets
            else None
        )
        required_models = {item.model for item in review.model_requirements}
        run_target_schema = MigrationRunTargetSchema.capture(
            run_id,
            target_schema,
            required_models,
        )
        run = MigrationRun(
            migration_run_id=run_id,
            project_id=project_id,
            data_version_id=data_version_id,
            run_number=self.repository.foundation.next_run_number(project_id),
            purpose=MigrationRunPurpose.TEST,
            label=clean_label,
            state=MigrationRunState.DRAFT,
            target_binding_id=target_binding_id,
            cutover_selection_id=None,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        target = RunTargetBinding(
            target_binding_id=target_binding_id,
            project_id=project_id,
            migration_run_id=run_id,
            environment="TEST",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=credential_generation,
            principal_hash=target_schema.read_principal_hash,
            permission_hash=target_schema.read_permission_hash,
            context_hash=target_schema.read_context_hash,
            schema_dependency_hash=run_target_schema.content_hash,
            reference_snapshot_hashes=tuple(
                item.content_hash for item in captured_reference_datasets
            ),
            created_at=now,
        )
        requirement_plan = MigrationRunRequirementPlan(
            migration_run_id=run_id,
            project_id=project_id,
            data_version_id=data_version_id,
            target_binding_id=target_binding_id,
            selected_revisions=tuple(
                item.selection for item in review.applications
            ),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            application_order=review.application_order,
            created_at=now,
        )
        planned = tuple(
            self._planned_application(
                item,
                run=run,
                target=target,
                now=now,
            )
            for item in review.applications
        )
        request_hash = content_hash(
            {
                "control_values": control_values or {},
                "data_version_id": data_version_id,
                "dependencies": [item.to_dict() for item in review.dependencies],
                "label": clean_label,
                "parameter_values": parameter_values or {},
                "project_id": project_id,
                "selected_revisions": [
                    item.selection.to_dict() for item in review.applications
                ],
                "reference_bundle": (
                    run_reference_bundle.to_portable_dict()
                    if run_reference_bundle is not None
                    else None
                ),
                "target_schema_hash": run_target_schema.content_hash,
            }
        )
        bundle = self.repository.provision_integrated_run(
            run=run,
            target_binding=target,
            requirement_plan=requirement_plan,
            applications=planned,
            target_schema=run_target_schema,
            reference_bundle=run_reference_bundle,
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )
        committed = self._materialize_applications(
            bundle,
            review=review,
            operation_id=operation_id,
            ready_event_type="INTEGRATED_TEST_RUN_READY",
            target_workspace_state=self.workspace_states.repository.get(
                target_schema.workspace_id
            ),
            actor=actor,
        )
        self.cutover_plans.ensure_for_run(
            project_id=project_id,
            migration_run_id=committed.run.migration_run_id,
            requirement_plan=committed.requirement_plan,
            write_ownership=tuple(
                sorted(
                    CutoverWriteOwnership(
                        recipe_id=item.selection.recipe_id,
                        model=model,
                        field=field,
                    )
                    for item in review.applications
                    for model, field in item.write_claims
                )
            ),
            operation_id=self._child_operation(operation_id, "cutover-plan"),
            actor=actor,
        )
        return committed

    def activate_test_run(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        test_binding: TestRunSetupBinding,
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Activate one fresh Test setup and create isolated Recipe work areas."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        credential_generation = required_text(
            credential_generation,
            "credential_generation",
            maximum=300,
        )
        if credential_generation != target_schema.read_credential_binding_hash:
            raise MigrationRunPlanningError(
                "The Test schema belongs to another read credential generation"
            )
        if (
            test_binding.project_id != project_id
            or target_schema.workspace_id != test_binding.setup_workspace_id
        ):
            raise MigrationRunPlanningError(
                "The reviewed Odoo target evidence does not belong to this Test setup"
            )
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in test_binding.selected_revisions
        )
        review = self.review_test_run(
            project_id,
            data_version_id=test_binding.data_version_id,
            recipe_revisions=selected,
            dependencies=test_binding.dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=None,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise MigrationRunPlanningError(
                f"{first.message} {first.recovery_action}"
            )
        if test_binding.state is TestRunSetupState.ACTIVE:
            resumed = self.repository.resume_test_activation(
                operation_id,
                actor=actor,
                fault=fault,
            )
            return self._materialize_applications(
                resumed,
                review=review,
                operation_id=operation_id,
                ready_event_type="INTEGRATED_TEST_RUN_READY",
                target_workspace_state=self.workspace_states.repository.get(
                    test_binding.setup_workspace_id
                ),
                actor=actor,
            )
        current_run = self.repository.foundation.get_migration_run(
            test_binding.migration_run_id
        )
        if (
            current_run.project_id != project_id
            or current_run.data_version_id != test_binding.data_version_id
            or current_run.purpose is not MigrationRunPurpose.TEST
            or current_run.target_binding_id is not None
        ):
            raise MigrationRunPlanningError(
                "Test run changed before activation; reload its setup"
            )
        now = utc_now()
        target_binding_id = self._child_operation(operation_id, "target-binding")
        run = replace(
            current_run,
            target_binding_id=target_binding_id,
            optimistic_revision=current_run.optimistic_revision + 1,
            updated_at=now,
        )
        required_reference_names = {
            item.name for item in review.reference_requirements
        }
        captured_reference_datasets = tuple(
            item
            for item in (
                target_reference_bundle.datasets
                if target_reference_bundle is not None
                else ()
            )
            if item.name in required_reference_names
        )
        run_reference_bundle = (
            MigrationRunReferenceBundle.capture(
                run.migration_run_id,
                target_reference_bundle,
                captured_reference_datasets,
            )
            if captured_reference_datasets
            else None
        )
        run_target_schema = MigrationRunTargetSchema.capture(
            run.migration_run_id,
            target_schema,
            {item.model for item in review.model_requirements},
        )
        target = RunTargetBinding(
            target_binding_id=target_binding_id,
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            environment="TEST",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=credential_generation,
            principal_hash=target_schema.read_principal_hash,
            permission_hash=target_schema.read_permission_hash,
            context_hash=target_schema.read_context_hash,
            schema_dependency_hash=run_target_schema.content_hash,
            reference_snapshot_hashes=tuple(
                item.content_hash for item in captured_reference_datasets
            ),
            created_at=now,
        )
        requirement_plan = MigrationRunRequirementPlan(
            migration_run_id=run.migration_run_id,
            project_id=project_id,
            data_version_id=run.data_version_id,
            target_binding_id=target_binding_id,
            selected_revisions=tuple(
                item.selection for item in review.applications
            ),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            application_order=review.application_order,
            created_at=now,
        )
        planned = tuple(
            self._planned_application(item, run=run, target=target, now=now)
            for item in review.applications
        )
        active_binding = replace(
            test_binding,
            state=TestRunSetupState.ACTIVE,
            target_binding_id=target_binding_id,
            activated_at=now,
        )
        bundle = self.repository.activate_test_run(
            run=run,
            test_binding=active_binding,
            target_binding=target,
            requirement_plan=requirement_plan,
            applications=planned,
            target_schema=run_target_schema,
            reference_bundle=run_reference_bundle,
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            request_hash=content_hash(
                {
                    "reference_bundle": (
                        run_reference_bundle.to_portable_dict()
                        if run_reference_bundle is not None
                        else None
                    ),
                    "target_schema_hash": run_target_schema.content_hash,
                    "test_setup_hash": test_binding.content_hash,
                    "parameter_values": parameter_values or {},
                }
            ),
            actor=actor,
            fault=fault,
        )
        committed = self._materialize_applications(
            bundle,
            review=review,
            operation_id=operation_id,
            ready_event_type="INTEGRATED_TEST_RUN_READY",
            target_workspace_state=self.workspace_states.repository.get(
                test_binding.setup_workspace_id
            ),
            actor=actor,
        )
        self.cutover_plans.ensure_for_run(
            project_id=project_id,
            migration_run_id=committed.run.migration_run_id,
            requirement_plan=committed.requirement_plan,
            write_ownership=tuple(
                sorted(
                    CutoverWriteOwnership(
                        recipe_id=item.selection.recipe_id,
                        model=model,
                        field=field,
                    )
                    for item in review.applications
                    for model, field in item.write_claims
                )
            ),
            operation_id=self._child_operation(operation_id, "cutover-plan"),
            actor=actor,
        )
        return committed

    def activate_production_run(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        production_binding: ProductionRunBinding,
        plan: CutoverPlanRevision,
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        test_connection_target_hash: str,
        read_credential_generation: str,
        write_identity: OdooWriteIdentity,
        write_credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        shared_control_values: Mapping[str, bool],
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Activate one setup run through the existing application compiler."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        read_credential_generation = required_text(
            read_credential_generation,
            "read_credential_generation",
            maximum=300,
        )
        write_credential_generation = required_text(
            write_credential_generation,
            "write_credential_generation",
            maximum=300,
        )
        if read_credential_generation != target_schema.read_credential_binding_hash:
            raise ProductionRunError(
                "The Production schema belongs to another read credential generation"
            )
        if read_credential_generation == write_credential_generation:
            raise ProductionRunError(
                "Production read and write credentials must remain separate"
            )
        if (
            write_identity.target_hash != target_schema.connection_target_hash
            or write_identity.context_hash != target_schema.read_context_hash
        ):
            raise ProductionRunError(
                "The Production write credential does not match the reviewed target context"
            )
        required_write_models = {
            item.model for item in plan.write_ownership
        }
        if not required_write_models.issubset(set(write_identity.writable_models)):
            raise ProductionRunError(
                "The Production write credential cannot update every planned Odoo model"
            )
        self.authorization.require(
            actor,
            Capability.PRODUCTION_RUN_ACTIVATE,
            project_id=project_id,
        )
        review = self.review_production_run(
            project_id,
            production_binding=production_binding,
            plan=plan,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            test_connection_target_hash=test_connection_target_hash,
            parameter_values=parameter_values,
            control_values=control_values,
            shared_control_values=shared_control_values,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise ProductionRunError(f"{first.message} {first.recovery_action}")
        if production_binding.state is ProductionRunBindingState.ACTIVE:
            self._assert_activation_retry_matches(
                production_binding,
                target_schema=target_schema,
                read_credential_generation=read_credential_generation,
                write_identity=write_identity,
                write_credential_generation=write_credential_generation,
                parameter_values=parameter_values,
                control_values=control_values,
                shared_control_values=shared_control_values,
            )
            resumed = self.repository.resume_production_activation(
                operation_id,
                actor=actor,
                fault=fault,
            )
            return self._materialize_applications(
                resumed,
                review=review,
                operation_id=operation_id,
                ready_event_type="PRODUCTION_RUN_READY",
                target_workspace_state=self.workspace_states.repository.get(
                    production_binding.setup_workspace_id
                ),
                actor=actor,
            )
        if production_binding.state is not ProductionRunBindingState.SETUP:
            raise ProductionRunError("Production run activation is inconsistent")
        current_run = self.repository.foundation.get_migration_run(
            production_binding.migration_run_id
        )
        if (
            current_run.project_id != project_id
            or current_run.data_version_id != production_binding.data_version_id
            or current_run.purpose is not MigrationRunPurpose.PRODUCTION
            or current_run.cutover_selection_id
            != production_binding.cutover_selection_id
            or current_run.target_binding_id is not None
        ):
            raise ProductionRunError(
                "Production run changed before activation; reload its setup"
            )
        now = utc_now()
        target_binding_id = self._child_operation(operation_id, "target-binding")
        run = replace(
            current_run,
            target_binding_id=target_binding_id,
            optimistic_revision=current_run.optimistic_revision + 1,
            updated_at=now,
        )
        required_reference_names = {
            item.name for item in review.reference_requirements
        }
        captured_reference_datasets = tuple(
            item
            for item in (
                target_reference_bundle.datasets
                if target_reference_bundle is not None
                else ()
            )
            if item.name in required_reference_names
        )
        run_reference_bundle = (
            MigrationRunReferenceBundle.capture(
                run.migration_run_id,
                target_reference_bundle,
                captured_reference_datasets,
            )
            if captured_reference_datasets
            else None
        )
        required_models = {item.model for item in review.model_requirements}
        run_target_schema = MigrationRunTargetSchema.capture(
            run.migration_run_id,
            target_schema,
            required_models,
        )
        target = RunTargetBinding(
            target_binding_id=target_binding_id,
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            environment="PRODUCTION",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=read_credential_generation,
            principal_hash=target_schema.read_principal_hash,
            permission_hash=target_schema.read_permission_hash,
            context_hash=target_schema.read_context_hash,
            schema_dependency_hash=run_target_schema.content_hash,
            reference_snapshot_hashes=tuple(
                item.content_hash for item in captured_reference_datasets
            ),
            created_at=now,
        )
        requirement_plan = MigrationRunRequirementPlan(
            migration_run_id=run.migration_run_id,
            project_id=project_id,
            data_version_id=run.data_version_id,
            target_binding_id=target_binding_id,
            selected_revisions=tuple(
                item.selection for item in review.applications
            ),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            application_order=review.application_order,
            created_at=now,
        )
        planned = tuple(
            self._planned_application(
                item,
                run=run,
                target=target,
                now=now,
            )
            for item in review.applications
        )
        parameter_hash = content_hash(parameter_values or {})
        control_hash = content_hash(
            {
                "recipe_controls": control_values or {},
                "shared_controls": dict(shared_control_values),
            }
        )
        write_identity_values = {
            "context_hash": write_identity.context_hash,
            "observed_at": write_identity.observed_at,
            "permission_hash": write_identity.permission_hash,
            "principal_hash": write_identity.principal_hash,
            "readable_models": sorted(set(write_identity.readable_models)),
            "target_hash": write_identity.target_hash,
            "writable_models": sorted(set(write_identity.writable_models)),
        }
        evidence_hash = activation_evidence_hash(
            binding=production_binding,
            target_binding_hash=target.content_hash,
            requirement_plan_hash=requirement_plan.content_hash,
            write_identity=write_identity_values,
            parameter_values_hash=parameter_hash,
            control_values_hash=control_hash,
        )
        active_binding = replace(
            production_binding,
            state=ProductionRunBindingState.ACTIVE,
            target_binding_id=target_binding_id,
            read_credential_generation=read_credential_generation,
            write_credential_generation=write_credential_generation,
            write_principal_hash=write_identity.principal_hash,
            write_permission_hash=write_identity.permission_hash,
            write_context_hash=write_identity.context_hash,
            parameter_values_hash=parameter_hash,
            control_values_hash=control_hash,
            activation_evidence_hash=evidence_hash,
            activated_at=now,
        )
        request_hash = content_hash(
            {
                "control_values": control_values or {},
                "cutover_selection_id": production_binding.cutover_selection_id,
                "data_version_id": run.data_version_id,
                "parameter_values": parameter_values or {},
                "production_setup_hash": production_binding.content_hash,
                "project_id": project_id,
                "read_credential_generation": read_credential_generation,
                "reference_bundle": (
                    run_reference_bundle.to_portable_dict()
                    if run_reference_bundle is not None
                    else None
                ),
                "shared_control_values": dict(shared_control_values),
                "target_schema_hash": run_target_schema.content_hash,
                "write_credential_generation": write_credential_generation,
                "write_identity": {
                    key: value
                    for key, value in write_identity_values.items()
                    if key != "observed_at"
                },
            }
        )
        bundle = self.repository.activate_production_run(
            run=run,
            production_binding=active_binding,
            target_binding=target,
            requirement_plan=requirement_plan,
            applications=planned,
            target_schema=run_target_schema,
            reference_bundle=run_reference_bundle,
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )
        return self._materialize_applications(
            bundle,
            review=review,
            operation_id=operation_id,
            ready_event_type="PRODUCTION_RUN_READY",
            target_workspace_state=self.workspace_states.repository.get(
                production_binding.setup_workspace_id
            ),
            actor=actor,
        )

    def _assert_activation_retry_matches(
        self,
        binding: ProductionRunBinding,
        *,
        target_schema: OdooSchemaCatalog,
        read_credential_generation: str,
        write_identity: OdooWriteIdentity,
        write_credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        shared_control_values: Mapping[str, bool],
    ) -> None:
        """Reject reuse of an activation identity with changed authority."""

        target = self.repository.get_target_binding(binding.migration_run_id)
        parameter_hash = content_hash(parameter_values or {})
        control_hash = content_hash(
            {
                "recipe_controls": control_values or {},
                "shared_controls": dict(shared_control_values),
            }
        )
        if (
            target.connection_target_hash != target_schema.connection_target_hash
            or target.principal_hash != target_schema.read_principal_hash
            or target.permission_hash != target_schema.read_permission_hash
            or target.context_hash != target_schema.read_context_hash
            or binding.read_credential_generation != read_credential_generation
            or binding.write_credential_generation != write_credential_generation
            or binding.write_principal_hash != write_identity.principal_hash
            or binding.write_permission_hash != write_identity.permission_hash
            or binding.write_context_hash != write_identity.context_hash
            or binding.parameter_values_hash != parameter_hash
            or binding.control_values_hash != control_hash
        ):
            raise ProductionRunError(
                "Production activation was already recorded with different evidence"
            )

    def _materialize_applications(
        self,
        bundle: IntegratedRunBundle,
        *,
        review: IntegratedRunReview,
        operation_id: str,
        ready_event_type: str,
        target_workspace_state,
        actor: Actor,
    ) -> IntegratedRunBundle:
        """Use the same source projection and mapping compiler for Test/Production."""

        package = self.source_packages.repository.get_source_package(
            bundle.run.data_version_id
        )
        if package is None:
            raise MigrationRunPlanningError("DataVersion source package is missing")
        reviewed = {item.selection.recipe_id: item for item in review.applications}
        workspace_by_id = {item.workspace_id: item for item in bundle.workspaces}
        project = self.projects.get(bundle.run.project_id, actor=actor)
        stored_applications = []
        for application in bundle.applications:
            item = reviewed[application.recipe_id]
            workspace = workspace_by_id[application.workspace_id]
            if item.assessment.dataset_ids:
                projection = (
                    self.source_projections.repository.get_workspace_source_projection(
                        workspace.workspace_id
                    )
                )
                if projection is None:
                    self.source_projections.materialize(
                        workspace.workspace_id,
                        actor=actor,
                        dataset_ids=item.assessment.dataset_ids,
                        expected_workspace_revision=workspace.optimistic_revision,
                        operation_id=self._child_operation(
                            operation_id,
                            f"source:{application.recipe_id}",
                        ),
                    )
            self._provision_engine(
                workspace,
                project=project,
                package=package,
                target_workspace_state=target_workspace_state,
                actor=actor,
            )
            materialized = self.compiler.materialize(
                workspace.workspace_id,
                application_id=application.application_id,
                recipe_id=application.recipe_id,
                data_version_id=application.data_version_id,
                definition=item.definition,
                assessment=item.assessment,
                actor=actor,
            )
            stored_applications.append(
                self.repository.save_application_materialization(
                    application.application_id,
                    expected_evidence_hash=application.evidence_hash,
                    status=materialized.status,
                    issues=materialized.issues,
                    mapping_id=materialized.mapping_id,
                    mapping_content_hash=materialized.mapping_content_hash,
                    evidence_hash=materialized.evidence_hash,
                    actor=actor,
                )
            )
        if stored_applications and all(
            item.status is RecipeApplicationStatus.READY
            for item in stored_applications
        ):
            current = self.repository.foundation.get_migration_run(
                bundle.run.migration_run_id
            )
            if current.state is MigrationRunState.DRAFT:
                self.repository.foundation.save_migration_run(
                    replace(
                        current,
                        state=MigrationRunState.READY,
                        updated_at=utc_now(),
                    ),
                    expected_revision=current.optimistic_revision,
                    event_type=ready_event_type,
                    actor=actor,
                )
        return self.repository.commit_provisioning(operation_id)

    def target_schema_from_workspace(
        self,
        project_id: str,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Read one reviewed target snapshot for run-level reuse."""

        return self.target_evidence_from_workspace(
            project_id,
            workspace_id,
            actor=actor,
        )[0]

    def confirm_application_odoo_defaults(
        self,
        application_id: str,
        *,
        actor: Actor,
    ) -> RunRecipeApplication:
        """Confirm the grouped target defaults already checked for one run."""

        application = self.repository.get_application(application_id)
        self.authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=application.project_id,
        )
        issues = self.repository.list_issues(application.application_id)
        default_reviews = tuple(
            item
            for item in issues
            if item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
            and item.level is MigrationRunPlanIssueLevel.REVIEW
        )
        other_actionable = tuple(
            item
            for item in issues
            if item not in default_reviews
            and item.level is not MigrationRunPlanIssueLevel.INFORMATION
        )
        if not default_reviews:
            raise MigrationRunPlanningError(
                "No reviewed Odoo defaults are waiting for confirmation"
            )
        if other_actionable:
            raise MigrationRunPlanningError(other_actionable[0].message)
        schema = self.compiler.schemas.get_odoo_schema_catalog(
            application.workspace_id
        )
        revision = self.compiler.mappings.mappings.get_mapping_revision(
            application.workspace_id
        )
        working = self.compiler.mappings.mappings.get_mapping_working_draft(
            application.workspace_id
        )
        if schema is None or revision is None or working is None:
            raise MigrationRunPlanningError(
                "Recheck Odoo and rebuild this Recipe application before "
                "confirming defaults"
            )
        fields_by_model = {
            model.name: {field.name: field for field in model.fields}
            for model in schema.models
        }
        mapped_default_fields = {
            (dataset.target_model, field.target_field)
            for dataset in revision.definition.datasets
            for field in dataset.fields
            if field.value_source is ScalarValueSource.ODOO_DEFAULT
        }
        mapped_default_fields.update(
            (dataset.target_model, disposition.target_field)
            for dataset in revision.definition.datasets
            for disposition in dataset.target_field_dispositions
            if disposition.handling is TargetFieldHandling.ODOO_DEFAULT
        )
        default_fields = tuple(
            sorted(
                (model_name, field_name)
                for model_name, field_name in mapped_default_fields
                if fields_by_model.get(model_name, {}).get(field_name) is not None
                and fields_by_model[model_name][field_name].required
                and fields_by_model[model_name][field_name].create_default_present
            )
        )
        if not default_fields or len(default_fields) != len(default_reviews):
            raise MigrationRunPlanningError(
                "One or more Odoo defaults are no longer verified for this target"
            )
        self.compiler.mappings.submit_current(
            application.workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=working.version,
            actor=actor,
        )
        remaining = tuple(
            item for item in issues if item not in default_reviews
        )
        evidence_hash = content_hash(
            {
                "application_id": application.application_id,
                "confirmed_odoo_defaults": [list(item) for item in default_fields],
                "mapping_content_hash": revision.definition.content_hash,
                "previous_evidence_hash": application.evidence_hash,
                "schema_hash": schema.content_hash,
                "status": RecipeApplicationStatus.READY.value,
            }
        )
        return self.repository.save_application_materialization(
            application.application_id,
            expected_evidence_hash=application.evidence_hash,
            status=RecipeApplicationStatus.READY,
            issues=remaining,
            mapping_id=revision.mapping_id,
            mapping_content_hash=revision.definition.content_hash,
            evidence_hash=evidence_hash,
            actor=actor,
        )

    def recover_blocked_test_run_defaults(
        self,
        migration_run_id: str,
        *,
        current_schema: OdooSchemaCatalog,
        actor: Actor,
    ) -> tuple[RunRecipeApplication, ...]:
        """Reassess old required-field blockers with fresh scalar defaults."""

        bundle = self.repository.get_bundle(migration_run_id)
        if bundle.run.purpose is not MigrationRunPurpose.TEST:
            raise MigrationRunPlanningError(
                "Required-field recovery is available here only for Test runs"
            )
        self.authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=bundle.run.project_id,
        )
        package = self.source_packages.repository.get_source_package(
            bundle.run.data_version_id
        )
        if package is None:
            raise MigrationRunPlanningError("DataVersion source package is missing")
        source_selection = self._package_selection(package)
        run_references = self.repository.get_run_reference_bundle(
            migration_run_id
        )
        current_models = {model.name: model for model in current_schema.models}
        recovered: list[RunRecipeApplication] = []
        for application in bundle.applications:
            if application.status is not RecipeApplicationStatus.BLOCKED:
                continue
            existing_issues = self.repository.list_issues(
                application.application_id
            )
            blockers = tuple(item for item in existing_issues if item.blocks)
            if not blockers or any(
                item.code != "RECIPE_TARGET_NEW_REQUIRED_FIELD"
                for item in blockers
            ):
                continue
            frozen_projection = self.repository.get_workspace_target_schema(
                application.workspace_id
            )
            if frozen_projection is None:
                continue
            selected_models = tuple(
                current_models[model.name]
                for model in frozen_projection.models
                if model.name in current_models
            )
            if len(selected_models) != len(frozen_projection.models):
                continue
            projection = replace(
                current_schema,
                workspace_id=application.workspace_id,
                models=selected_models,
                content_hash=content_hash(
                    {
                        "application_id": application.application_id,
                        "current_schema_hash": current_schema.content_hash,
                        "frozen_projection_hash": frozen_projection.content_hash,
                        "kind": "RUN_CREATE_DEFAULT_PROJECTION",
                    }
                ),
                pending_refresh=None,
            )
            envelope = self.recipes.read_revision(
                application.recipe_id,
                application.recipe_revision,
                actor=actor,
            )
            definition = dict(envelope["recipe"])
            assessment = self.compiler.assess(
                recipe_id=application.recipe_id,
                definition=definition,
                source_selection=source_selection,
                target_schema=projection,
                reference_bundle=(
                    run_references.for_workspace(application.workspace_id)
                    if run_references is not None
                    else None
                ),
                parameter_values={},
                control_values={},
            )
            legacy_binding_hash = content_hash(
                {
                    "control_values": dict(
                        sorted(assessment.control_values.items())
                    ),
                    "parameter_values": dict(
                        sorted(assessment.parameter_values.items())
                    ),
                    "source_bindings": dict(
                        sorted(assessment.source_bindings.items())
                    ),
                }
            )
            if (
                assessment.blocked
                or not assessment.target_default_fields
                or application.physical_binding_hash
                not in {assessment.physical_binding_hash, legacy_binding_hash}
            ):
                continue
            save_projection = getattr(
                self.compiler.schemas,
                "save_run_default_projection",
                None,
            )
            if save_projection is None:
                raise MigrationRunPlanningError(
                    "Run default projection storage is unavailable"
                )
            save_projection(application.workspace_id, projection, actor=actor)
            materialized = self.compiler.materialize(
                application.workspace_id,
                application_id=application.application_id,
                recipe_id=application.recipe_id,
                data_version_id=application.data_version_id,
                definition=definition,
                assessment=assessment,
                actor=actor,
            )
            recovered.append(
                self.repository.save_application_materialization(
                    application.application_id,
                    expected_evidence_hash=application.evidence_hash,
                    status=materialized.status,
                    issues=materialized.issues,
                    mapping_id=materialized.mapping_id,
                    mapping_content_hash=materialized.mapping_content_hash,
                    evidence_hash=materialized.evidence_hash,
                    actor=actor,
                )
            )
        return tuple(recovered)

    def target_evidence_from_workspace(
        self,
        project_id: str,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[OdooSchemaCatalog, ReferenceBundle | None]:
        """Read one reviewed schema and reference package for run-level reuse."""

        project_id = require_uuid(project_id, "project_id")
        workspace_id = require_uuid(workspace_id, "workspace_id")
        workspace = self.repository.foundation.get_migration_workspace(workspace_id)
        if workspace.project_id != project_id:
            raise MigrationRunPlanningError(
                "The selected Odoo evidence belongs to another Project"
            )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        schema = self.compiler.schemas.get_odoo_schema_catalog(workspace_id)
        if schema is None or schema.origin.value != "LIVE_API":
            raise MigrationRunPlanningError(
                "Capture authenticated Odoo 19 evidence in the authoring workspace first"
            )
        try:
            major = int(str(schema.odoo_version).split(".", 1)[0])
        except ValueError:
            major = -1
        if major != 19:
            raise MigrationRunPlanningError(
                "The selected target evidence is not from Odoo 19"
            )
        references = self.compiler.references.get_reference_bundle(workspace_id)
        return schema, references

    def _planned_application(
        self,
        item: ReviewedRecipeApplication,
        *,
        run: MigrationRun,
        target: RunTargetBinding,
        now: datetime,
    ) -> PlannedRecipeApplication:
        application_id = str(uuid4())
        workspace_id = str(uuid4())
        issues = item.assessment.issues
        status = (
            RecipeApplicationStatus.BLOCKED
            if item.assessment.blocked
            else RecipeApplicationStatus.DRAFT_READINESS
        )
        issue_hash = content_hash([value.to_dict() for value in issues])
        evidence_hash = content_hash(
            {
                "application_id": application_id,
                "issue_hash": issue_hash,
                "physical_binding_hash": item.assessment.physical_binding_hash,
                "recipe": item.selection.to_dict(),
                "status": status.value,
                "target_binding_hash": target.content_hash,
            }
        )
        application = RunRecipeApplication(
            application_id=application_id,
            project_id=run.project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=run.data_version_id,
            workspace_id=workspace_id,
            recipe_id=item.selection.recipe_id,
            recipe_revision=item.selection.recipe_revision,
            recipe_semantic_hash=item.selection.semantic_hash,
            target_binding_id=target.target_binding_id,
            physical_binding_hash=item.assessment.physical_binding_hash,
            parameter_values_hash=item.assessment.parameter_values_hash,
            status=status,
            issue_hash=issue_hash,
            mapping_id=None,
            mapping_content_hash=None,
            evidence_hash=evidence_hash,
            created_at=now,
            updated_at=now,
        )
        workspace = MigrationWorkspace(
            workspace_id=workspace_id,
            project_id=run.project_id,
            data_version_id=run.data_version_id,
            migration_run_id=run.migration_run_id,
            recipe_application_id=application_id,
            display_name=(
                f"{item.recipe.display_name} "
                f"{run.purpose.value.title()} application"
            ),
            state=MigrationWorkspaceState.OPEN,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        return PlannedRecipeApplication(
            application=application,
            workspace=workspace,
            dataset_ids=item.assessment.dataset_ids,
            requirements=item.requirements,
            reference_requirements=item.reference_requirements,
            issues=issues,
        )

    def _provision_engine(
        self,
        workspace: MigrationWorkspace,
        *,
        project,
        package: DataVersionSourcePackage,
        target_workspace_state,
        actor: Actor,
    ) -> None:
        try:
            current = self.workspace_states.repository.get(workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            source_mode = (
                SourceMode.FILE
                if package.origin is SourcePackageOrigin.FILE
                else SourceMode.ODOO
            )
            current = self.workspace_states.provision_migration_workspace(
                workspace.workspace_id,
                actor=actor,
                name=workspace.display_name,
                source_system=project.source_system_identity,
                source_mode=source_mode,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        if (
            target_workspace_state.odoo_connection_mode is not None
            and (
                current.odoo_connection_mode
                != target_workspace_state.odoo_connection_mode
                or current.odoo_base_url != target_workspace_state.odoo_base_url
                or current.odoo_database != target_workspace_state.odoo_database
                or current.intended_applications
                != target_workspace_state.intended_applications
                or current.intended_models != target_workspace_state.intended_models
            )
        ):
            self.workspace_states.update_target(
                current.workspace_id,
                actor=actor,
                expected_revision=current.revision,
                odoo_connection_mode=(
                    target_workspace_state.odoo_connection_mode.value
                ),
                odoo_base_url=target_workspace_state.odoo_base_url,
                odoo_database=target_workspace_state.odoo_database,
                intended_applications=target_workspace_state.intended_applications,
                intended_models=target_workspace_state.intended_models,
            )

    @staticmethod
    def _package_selection(
        package: DataVersionSourcePackage,
    ) -> DataVersionDatasetView:
        datasets = tuple(item.to_mapping_dataset() for item in package.datasets)
        return DataVersionDatasetView(
            data_version_id=package.data_version_id,
            package_hash=package.content_hash,
            datasets=datasets,
        )

    @staticmethod
    def _union_requirements(
        applications: list[ReviewedRecipeApplication],
    ) -> tuple[OdooModelRequirement, ...]:
        by_model: dict[str, set[str]] = {}
        for item in applications:
            for requirement in item.requirements:
                by_model.setdefault(requirement.model, set()).update(
                    requirement.fields
                )
        return tuple(
            OdooModelRequirement(model=model, fields=tuple(fields))
            for model, fields in sorted(by_model.items())
        )

    @staticmethod
    def _union_reference_requirements(
        applications: list[ReviewedRecipeApplication],
        issues: list[MigrationRunPlanIssue],
    ) -> tuple[ReferenceRequirement, ...]:
        by_name: dict[str, ReferenceRequirement] = {}
        owners: dict[str, str] = {}
        for application in applications:
            for requirement in application.reference_requirements:
                current = by_name.get(requirement.name)
                if current is None:
                    by_name[requirement.name] = requirement
                    owners[requirement.name] = application.selection.recipe_id
                    continue
                if current.content_hash != requirement.content_hash:
                    issues.append(
                        MigrationRunPlanningService._block(
                            "RUN_REFERENCE_REQUIREMENT_COLLISION",
                            (
                                f"Two Recipes require different versions of "
                                f"reference data {requirement.name}."
                            ),
                            (
                                "Publish compatible Recipe revisions or use "
                                "one shared reviewed reference version."
                            ),
                            (
                                owners[requirement.name],
                                application.selection.recipe_id,
                            ),
                        )
                    )
        return tuple(sorted(by_name.values()))

    @staticmethod
    def _semantic_requirement_hash(review: IntegratedRunReview) -> str:
        """Match the reusable requirement meaning stored by the CutoverPlan."""

        return content_hash(
            {
                "application_order": list(review.application_order),
                "contract_version": 1,
                "dependencies": [
                    item.to_dict() for item in review.dependencies
                ],
                "model_requirements": [
                    item.to_dict() for item in review.model_requirements
                ],
                "reference_requirements": [
                    item.to_dict() for item in review.reference_requirements
                ],
                "selected_revisions": [
                    item.selection.to_dict() for item in review.applications
                ],
            }
        )

    @staticmethod
    def _write_collision_issues(applications, issues) -> None:
        owners: dict[tuple[str, str], str] = {}
        for item in applications:
            for claim in item.write_claims:
                previous = owners.setdefault(claim, item.selection.recipe_id)
                if previous != item.selection.recipe_id:
                    issues.append(
                        MigrationRunPlanningService._block(
                            "RUN_RECIPE_WRITE_COLLISION",
                            (
                                f"Two Recipes may both write {claim[0]}.{claim[1]}."
                            ),
                            (
                                "Choose one owning Recipe for this Odoo field or "
                                "publish non-overlapping Recipe meaning. Reordering "
                                "does not resolve the collision."
                            ),
                            (previous, item.selection.recipe_id),
                        )
                    )

    @staticmethod
    def _application_order(selected_ids, dependencies, issues) -> tuple[str, ...]:
        following = {recipe_id: set() for recipe_id in selected_ids}
        indegree = {recipe_id: 0 for recipe_id in selected_ids}
        seen = set()
        for edge in dependencies:
            key = (edge.before_recipe_id, edge.after_recipe_id)
            if key in seen:
                issues.append(
                    MigrationRunPlanningService._block(
                        "RUN_DEPENDENCY_DUPLICATED",
                        "The same Recipe dependency was selected more than once.",
                        "Keep one copy of each dependency.",
                        key,
                    )
                )
                continue
            seen.add(key)
            if (
                edge.before_recipe_id not in selected_ids
                or edge.after_recipe_id not in selected_ids
            ):
                issues.append(
                    MigrationRunPlanningService._block(
                        "RUN_DEPENDENCY_RECIPE_MISSING",
                        "A dependency refers to a Recipe outside this Test run.",
                        "Select both Recipes or remove that dependency.",
                        key,
                    )
                )
                continue
            following[edge.before_recipe_id].add(edge.after_recipe_id)
            indegree[edge.after_recipe_id] += 1
        ready = sorted(recipe_id for recipe_id, count in indegree.items() if count == 0)
        order = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for after in sorted(following[current]):
                indegree[after] -= 1
                if indegree[after] == 0:
                    ready.append(after)
                    ready.sort()
        if len(order) != len(selected_ids):
            cycle_ids = tuple(sorted(key for key, value in indegree.items() if value))
            issues.append(
                MigrationRunPlanningService._block(
                    "RUN_RECIPE_DEPENDENCY_CYCLE",
                    "The selected Recipes form a dependency cycle.",
                    "Remove one dependency so the applications have a clear order.",
                    cycle_ids,
                )
            )
            return tuple(sorted(selected_ids))
        return tuple(order)

    @staticmethod
    def _block(code, message, recovery, recipe_ids):
        return MigrationRunPlanIssue(
            code=code,
            level=MigrationRunPlanIssueLevel.BLOCKER,
            message=message,
            recovery_action=recovery,
            recipe_ids=tuple(recipe_ids),
        )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))

