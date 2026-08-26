"""Stable facade for focused integrated-run application capabilities."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.access import Actor, AuthorizationPolicy
from impodo.application.data_version.service import DataVersionService
from impodo.application.project.service import MigrationProjectService
from impodo.application.recipe.service import RecipeService
from impodo.application.recipe_application_service import RecipeApplicationService
from impodo.data_version_sources import (
    DataVersionDatasetView,
    DataVersionSourcePackage,
    WorkspaceSourceProjectionService,
)
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.cutover.models import CutoverPlanRevision
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.migration_foundation import FaultInjector
from impodo.migration_production import ProductionRunBinding
from impodo.migration_run_planning import (
    IntegratedRunBundle,
    MigrationRunPlanningError,
    RecipeDependency,
    RunRecipeApplication,
)
from impodo.migration_test import TestRunSetupBinding
from impodo.models import OdooWriteIdentity
from impodo.workspace_contracts import OdooSchemaCatalog
from impodo.workspace_state import WorkspaceStateService

from .application_materialization import RunApplicationMaterializer
from .application_recovery import RunApplicationRecoveryUseCase
from .planning_models import IntegratedRunReview, ReviewedRecipeApplication
from .production_activation import ProductionRunActivationUseCase
from .production_review import ProductionRunReviewUseCase
from .review import RunReviewUseCase
from .target_evidence import RunTargetEvidenceUseCase
from .test_activation import TestRunActivationUseCase


class MigrationRunPlanningService:
    """Compatibility facade over focused run review and activation use cases."""

    def __init__(
        self,
        *,
        projects: MigrationProjectService,
        data_versions: DataVersionService,
        recipes: RecipeService,
        repository,
        test_run_values,
        source_packages,
        source_projections: WorkspaceSourceProjectionService,
        workspace_states: WorkspaceStateService,
        compiler: RecipeApplicationService,
        cutover_plans,
        authorization: AuthorizationPolicy,
    ) -> None:
        # Preserve the established composition surface while operation logic lives
        # in the focused collaborators below.
        self.projects = projects
        self.data_versions = data_versions
        self.recipes = recipes
        self.repository = repository
        self.test_run_values = test_run_values
        self.source_packages = source_packages
        self.source_projections = source_projections
        self.workspace_states = workspace_states
        self.compiler = compiler
        self.cutover_plans = cutover_plans
        self.authorization = authorization

        self._target_evidence = RunTargetEvidenceUseCase(
            foundation=repository.foundation,
            compiler=compiler,
            authorization=authorization,
            planning_error=MigrationRunPlanningError,
        )
        self._review = RunReviewUseCase(
            projects=projects,
            data_versions=data_versions,
            recipes=recipes,
            repository=repository,
            source_packages=source_packages,
            compiler=compiler,
            authorization=authorization,
            planning_error=MigrationRunPlanningError,
            reviewed_application_type=ReviewedRecipeApplication,
            review_type=IntegratedRunReview,
            package_selection=self._package_selection,
        )
        self._production_review = ProductionRunReviewUseCase(review=self._review)
        self._application_recovery = RunApplicationRecoveryUseCase(
            repository=repository,
            authorization=authorization,
            compiler=compiler,
            source_packages=source_packages,
            data_versions=data_versions,
            test_run_values=test_run_values,
            recipes=recipes,
            package_selection=self._package_selection,
        )
        materializer = RunApplicationMaterializer(
            repository=repository,
            source_packages=source_packages,
            projects=projects,
            source_projections=source_projections,
            workspace_states=workspace_states,
            compiler=compiler,
        )
        self._test_activation = TestRunActivationUseCase(
            repository=repository,
            review=self._review,
            materializer=materializer,
            workspace_states=workspace_states,
            cutover_plans=cutover_plans,
            authorization=authorization,
        )
        self._production_activation = ProductionRunActivationUseCase(
            repository=repository,
            review=self._production_review,
            materializer=materializer,
            workspace_states=workspace_states,
            authorization=authorization,
        )

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
        """Validate Test dependency and write ownership before provisioning."""

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
        """Review rollout evidence against exact qualified plan meaning."""

        return self._production_review.review(
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
        return self._review.review(
            project_id,
            data_version_id=data_version_id,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            purpose=purpose,
            required_target_workspace_id=required_target_workspace_id,
            actor=actor,
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
        return self._test_activation.start(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            data_version_id=data_version_id,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            credential_generation=credential_generation,
            label=label,
            operation_id=operation_id,
            actor=actor,
            parameter_values=parameter_values,
            control_values=control_values,
            fault=fault,
        )

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
        return self._test_activation.activate(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            test_binding=test_binding,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            credential_generation=credential_generation,
            parameter_values=parameter_values,
            operation_id=operation_id,
            actor=actor,
            fault=fault,
        )

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
        return self._production_activation.activate(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            production_binding=production_binding,
            plan=plan,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            test_connection_target_hash=test_connection_target_hash,
            read_credential_generation=read_credential_generation,
            write_identity=write_identity,
            write_credential_generation=write_credential_generation,
            parameter_values=parameter_values,
            control_values=control_values,
            shared_control_values=shared_control_values,
            operation_id=operation_id,
            actor=actor,
            fault=fault,
        )

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
        return self._application_recovery.confirm_odoo_defaults(
            application_id,
            actor=actor,
        )

    def confirm_application_mapping(
        self,
        application_id: str,
        *,
        actor: Actor,
    ) -> RunRecipeApplication:
        return self._application_recovery.confirm_mapping(
            application_id,
            actor=actor,
        )

    def recover_blocked_test_run_defaults(
        self,
        migration_run_id: str,
        *,
        current_schema: OdooSchemaCatalog,
        actor: Actor,
    ) -> tuple[RunRecipeApplication, ...]:
        return self._application_recovery.recover_blocked_test_defaults(
            migration_run_id,
            current_schema=current_schema,
            actor=actor,
        )

    def target_evidence_from_workspace(
        self,
        project_id: str,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[OdooSchemaCatalog, ReferenceBundle | None]:
        return self._target_evidence.read(
            project_id,
            workspace_id,
            actor=actor,
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
