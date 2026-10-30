"""Build deployment-neutral application service groups from injected ports.

The local and server composition roots may choose different persistence,
identity, and authorization adapters. They share these builders so those
deployment choices do not duplicate application-service wiring.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from impodo.application.business_intent_service import (
    BusinessIntentRepository as BusinessIntentRepositoryPort,
)
from impodo.application.business_intent_service import BusinessIntentService
from impodo.application.correction_execution import (
    CorrectionExecutionRepository,
    CorrectionExecutionService,
)
from impodo.application.correction_orchestration import (
    CorrectionAuthoringStageCoordinator,
    CorrectionBindingRepository,
    CorrectionMappingSeedService,
    CorrectionOriginPublisher,
    CorrectionProtectedStore,
    CorrectionReviewOrchestrator,
    CorrectionReviewPipeline,
    CorrectionSuccessorService,
    CorrectionTargetReviewStage,
)
from impodo.application.correction_stages import (
    CorrectionPreparationEvidenceReader,
    CurrentCorrectionMappingReviewStage,
    CurrentCorrectionPreparationReviewStage,
    CurrentCorrectionQualityReviewStage,
)
from impodo.application.correction_workflow import (
    CorrectionExecutionEvidenceReader,
    CorrectionPreparedEvidenceReader,
    CorrectionTargetSnapshotReader,
    CorrectionWorkflowService,
)
from impodo.application.cutover_plan_service import (
    CutoverPlanService,
    WorkspaceIntegratedQualificationEvidenceReader,
)
from impodo.application.data_version.ports import DataVersionRepository
from impodo.application.data_version.service import DataVersionService
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackageService,
    DataVersionSourceRepository,
    WorkspaceSourceProjectionRepository,
    WorkspaceSourceProjectionService,
)
from impodo.application.fallout_workbook_port import FalloutWorkbookRenderer
from impodo.application.fallout_workbook_service import (
    FalloutSourceReader,
    FalloutStagingReader,
    FalloutWorkbookService,
    FalloutWorkspaceReader,
)
from impodo.application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from impodo.application.odoo_capture_filters import OdooCaptureFilterStore
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.odoo_source_restart_service import OdooSourceRestartService
from impodo.application.preflight_service import PreflightService
from impodo.application.production_cutover_service import ProductionCutoverService
from impodo.application.project.ports import MigrationProjectRepository
from impodo.application.project.service import MigrationProjectService
from impodo.application.recipe.ports import RecipeRepository
from impodo.application.recipe.service import RecipeService
from impodo.application.recipe_application_service import RecipeApplicationService
from impodo.application.recipe_compilation_service import (
    RecipeAuthoringMappingRepository,
    RecipeAuthoringParameterRepository,
    RecipeAuthoringPreparationRepository,
    RecipeAuthoringQualityRepository,
    RecipeAuthoringReferenceRepository,
    RecipeAuthoringSchemaRepository,
    RecipeAuthoringSourceRepository,
    RecipeCompiler,
)
from impodo.application.recipe_publication_service import RecipePublicationService
from impodo.application.reconciliation_evidence_service import (
    ReconciliationEvidenceService,
)
from impodo.application.run.planning_service import MigrationRunPlanningService
from impodo.application.run.ports import (
    MigrationRunRepository,
    ProductionRunRepository,
    RunCutoverPlanRepository,
    RunPlanningRepository,
    TestRunRepository,
)
from impodo.application.run.service import MigrationRunService
from impodo.application.run.setup_service import RunSetupService
from impodo.application.run.test_setup_service import TestRunSetupService
from impodo.application.schema_workspace_service import (
    SchemaWorkspaceReader,
    SchemaWorkspaceRepository,
    SchemaWorkspaceService,
    SourceSelectionReader,
)
from impodo.application.shared.artifacts import (
    GovernedArtifactStores,
    WorkspaceArtifactStore,
)
from impodo.application.source_workspace_service import (
    OdooCaptureSchemaReader,
    SourceWorkspaceRepository,
    SourceWorkspaceService,
    WorkspaceStateReader,
)
from impodo.application.supporting_mapping_guard import (
    SupportingMappingGuard,
    SupportingPreparationPolicy,
)
from impodo.application.transfer_execution_service import (
    TransferExecutionService,
    TransferExecutionSourceRepository,
)
from impodo.application.workspace.access import (
    WorkspaceAccessRepository,
    WorkspaceAccessService,
)
from impodo.application.workspace.derived_entities import (
    DerivedEntityRepository,
    DerivedEntityWorkspaceService,
    ExactJoinGroupEvaluator,
)
from impodo.application.workspace.execution.reconciliation import (
    ReconciliationExecutionRepository,
    ReconciliationResultRepository,
    ReconciliationSchemaReader,
    ReconciliationService,
)
from impodo.application.workspace.execution.service import (
    ExecutionJournalRepository,
    ExecutionService,
    ExecutionWorkspaceRepository,
    ReadCredentialBindingProvider,
)
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
    CategoricalSourceRepository,
)
from impodo.application.workspace.mapping.logical_source_values import (
    LogicalSourceValues,
)
from impodo.application.workspace.mapping.numeric_precision_coverage import (
    NumericPrecisionCoverageService,
    NumericPrecisionSourceRepository,
)
from impodo.application.workspace.mapping.order_service import (
    MatchingOrderPreferenceRepository,
    MatchingOrderService,
)
from impodo.application.workspace.mapping.row_inclusion_review import (
    RowInclusionReviewRepository,
    RowInclusionReviewService,
)
from impodo.application.workspace.mapping.service import (
    MappingDownstreamInvalidator,
    MappingRowInclusionConfirmationRepository,
    MappingSchemaRepository,
    MappingSourceRepository,
    MappingWorkspaceRepository,
    MappingWorkspaceService,
    RecipeMappingPolicy,
)
from impodo.application.workspace.mapping.supporting_values import (
    MappingSupportingLookupReader,
)
from impodo.application.workspace.mapping.transformation_impact import (
    TransformationImpactDerivedRepository,
    TransformationImpactMappingRepository,
    TransformationImpactRepository,
    TransformationImpactService,
    TransformationImpactSourceRepository,
    TransformationImpactWorkspaceRepository,
)
from impodo.application.workspace.ports import MigrationWorkspaceRepository
from impodo.application.workspace.preparation.columnar_transformation_port import (
    ColumnarTransformationPort,
)
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    GroupedExactJoinSourcePort,
)
from impodo.application.workspace.preparation.normalization_service import (
    NormalizationService,
)
from impodo.application.workspace.preparation.preparation_service import (
    PreparationService,
)
from impodo.application.workspace.preparation.quality_service import (
    QualityService,
    RecipeQualitySeedRepository,
)
from impodo.application.workspace.preparation.readiness_ports import (
    NormalizationRepository,
    PreflightEffectiveRepository,
    PreflightMappingRepository,
    PreflightNormalizationRepository,
    PreflightQualityRepository,
    PreflightRepository,
    PreflightSchemaRepository,
    PreflightSourceRepository,
    PreflightStagingRepository,
    PreflightWorkspaceRepository,
    PreparationDerivedRepository,
    PreparationMappingRepository,
    PreparationSessionRepository,
    PreparationSourceRepository,
    PreparationStagingRepository,
    PreparationWorkspaceRepository,
    QualityMappingRepository,
    QualityRepository,
    QualitySourceRepository,
)
from impodo.application.workspace.preparation.resolution_service import (
    ResolutionRepository,
    ResolutionService,
    ResolutionStagingRepository,
)
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.preparation.quality import QualityRule
from impodo.domain.reconciliation import ReconciliationRun
from impodo.domain.reconciliation_detail import ReconciliationDetailManifest
from impodo.domain.run.setup import (
    MigrationRunTargetSetupRepository,
    MigrationRunTargetSetupService,
)
from impodo.domain.shared.access import (
    Actor,
    AuthorizationPolicy,
    WorkspaceAuthorizationPolicy,
)
from impodo.domain.workspace.workbench import (
    WorkspaceStateRepository,
    WorkspaceStateService,
)


class FoundationServiceRepository(
    MigrationProjectRepository,
    DataVersionRepository,
    MigrationRunRepository,
    MigrationRunTargetSetupRepository,
    MigrationWorkspaceRepository,
    DataVersionSourceRepository,
    WorkspaceAccessRepository,
    Protocol,
):
    """Combine the narrow ports required by the Project foundation services."""


class PreparationWorkspacePorts(
    PreparationWorkspaceRepository,
    PreflightWorkspaceRepository,
    Protocol,
):
    """Combine Workspace reads shared by preparation and final review."""


class PreparationSourcePorts(
    PreparationSourceRepository,
    QualitySourceRepository,
    PreflightSourceRepository,
    Protocol,
):
    """Combine source-evidence reads shared by the preparation services."""


class PreparationMappingPorts(
    PreparationMappingRepository,
    QualityMappingRepository,
    PreflightMappingRepository,
    Protocol,
):
    """Combine mapping reads shared by preparation and final review."""


class PreparationStagingPorts(
    PreparationStagingRepository,
    ResolutionStagingRepository,
    PreflightStagingRepository,
    Protocol,
):
    """Combine canonical-staging publication and review reads."""


class ExecutionWorkspacePorts(
    ExecutionWorkspaceRepository,
    FalloutWorkspaceReader,
    Protocol,
):
    """Combine Workspace state reads used by loading and fallout evidence."""


class ExecutionSourcePorts(
    TransferExecutionSourceRepository,
    FalloutSourceReader,
    Protocol,
):
    """Combine frozen source reads used by transfer loading and fallout evidence."""


class ExecutionJournalPorts(
    ExecutionJournalRepository,
    ReconciliationExecutionRepository,
    Protocol,
):
    """Combine journal commands with the reconciliation read surface."""


class CorrectionPreparedEvidencePorts(
    CorrectionPreparationEvidenceReader,
    CorrectionPreparedEvidenceReader,
    Protocol,
):
    """Read prepared evidence required by correction origin and review."""


class CorrectionExecutionPorts(
    CorrectionExecutionRepository,
    CorrectionExecutionEvidenceReader,
    Protocol,
):
    """Write correction journals and read completed execution evidence."""


class CorrectionReconciliationPorts(Protocol):
    """Publish and read correction reconciliation results."""

    def publish(
        self,
        workspace_id: str,
        report: ReconciliationRun,
        *,
        actor: Actor,
        detail: ReconciliationDetailManifest | None = None,
    ) -> None: ...

    def get_current(
        self,
        workspace_id: str,
        execution_run_id: str | None = None,
    ) -> ReconciliationRun | None: ...


CorrectionReviewPipelineFactory = Callable[
    [CorrectionAuthoringStageCoordinator],
    CorrectionReviewPipeline,
]


class PreparationQualityPorts(
    QualityRepository,
    PreflightQualityRepository,
    Protocol,
):
    """Combine quality publication and final-review reads."""


class PreparationNormalizationPorts(
    NormalizationRepository,
    PreflightNormalizationRepository,
    Protocol,
):
    """Combine normalization commands and final-review reads."""


class PreparationResolutionPorts(
    ResolutionRepository,
    PreflightEffectiveRepository,
    Protocol,
):
    """Combine duplicate resolution and effective-data reads."""


class AuthoringFoundationRepository(
    WorkspaceSourceProjectionRepository,
    Protocol,
):
    """Persist the immutable DataVersion projection used by a Workspace."""


class AuthoringMappingSourcePorts(
    RecipeAuthoringSourceRepository,
    MappingSourceRepository,
    Protocol,
):
    """Expose the effective physical-plus-derived mapping selection."""


class AuthoringWorkspacePorts(
    WorkspaceStateReader,
    SchemaWorkspaceReader,
    TransformationImpactWorkspaceRepository,
    Protocol,
):
    """Combine Workspace state reads used across source and Match data."""


class AuthoringSourcePorts(
    SourceWorkspaceRepository,
    SourceSelectionReader,
    CategoricalSourceRepository,
    NumericPrecisionSourceRepository,
    TransformationImpactSourceRepository,
    Protocol,
):
    """Combine immutable source-evidence reads and source commands."""


class AuthoringSchemaPorts(
    OdooCaptureSchemaReader,
    SchemaWorkspaceRepository,
    MappingSchemaRepository,
    RecipeAuthoringSchemaRepository,
    Protocol,
):
    """Combine schema capture, governance, mapping, and Recipe reads."""


class AuthoringMappingPorts(
    RecipeAuthoringMappingRepository,
    MappingWorkspaceRepository,
    TransformationImpactMappingRepository,
    Protocol,
):
    """Combine Match-data command and immutable evidence ports."""


class AuthoringDerivedPorts(
    RecipeAuthoringPreparationRepository,
    DerivedEntityRepository,
    TransformationImpactDerivedRepository,
    Protocol,
):
    """Combine derived-data reads used by authoring and impact review."""


class AuthoringRowInclusionPorts(
    RowInclusionReviewRepository,
    MappingRowInclusionConfirmationRepository,
    Protocol,
):
    """Combine row-inclusion review persistence and submission proof."""


class AuthoringMatchingOrderPorts(
    MatchingOrderPreferenceRepository,
    Protocol,
):
    """Persist matching-order preferences and live-check attempts."""


class AuthoringRecipeApplicationState(RecipeMappingPolicy, Protocol):
    """Persist one Recipe application's mapping policy and quality seed."""

    def save_quality_seed(
        self,
        workspace_id: str,
        *,
        application_id: str,
        mapping_content_hash: str,
        mapping_definition: MappingDefinition,
        rules: tuple[QualityRule, ...],
        actor: Actor,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FoundationServiceGraph:
    """Hold one consistently wired Project foundation service group."""

    workspace_access: WorkspaceAccessService
    workspace_states: WorkspaceStateService
    migration_projects: MigrationProjectService
    data_versions: DataVersionService
    migration_runs: MigrationRunService
    migration_run_target_setup: MigrationRunTargetSetupService
    migration_workspaces: MigrationWorkspaceService
    source_packages: DataVersionSourcePackageService
    project_authoring: MigrationProjectAuthoringService
    odoo_source_restarts: OdooSourceRestartService


@dataclass(frozen=True, slots=True)
class AuthoringServiceGraph:
    """Hold source, Recipe, and Match-data services for either deployment."""

    recipe_compiler: RecipeCompiler
    recipes: RecipeService
    recipe_publication: RecipePublicationService
    source_workspace: SourceWorkspaceService
    source_projections: WorkspaceSourceProjectionService
    data_version_source_projection: WorkspaceDataVersionSourceService
    categorical_coverage: CategoricalCoverageService
    numeric_precision_coverage: NumericPrecisionCoverageService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    transformation_impacts: TransformationImpactService
    row_inclusion_reviews: RowInclusionReviewService
    matching_order: MatchingOrderService
    recipe_application: RecipeApplicationService
    business_intents: BusinessIntentService


@dataclass(frozen=True, slots=True)
class RunServiceGraph:
    """Hold integrated run planning and setup services for either deployment."""

    run_planning: MigrationRunPlanningService
    test_runs: TestRunSetupService
    production_runs: ProductionCutoverService
    run_setups: RunSetupService


@dataclass(frozen=True, slots=True)
class ExecutionServiceGraph:
    """Hold load, transfer, reconciliation, and fallout services for a deployment."""

    execution: ExecutionService
    transfer_execution: TransferExecutionService
    reconciliation: ReconciliationService
    fallout_workbooks: FalloutWorkbookService


@dataclass(frozen=True, slots=True)
class CutoverServiceGraph:
    """Hold integrated Test qualification services for either deployment."""

    qualification_evidence: WorkspaceIntegratedQualificationEvidenceReader
    cutover_plans: CutoverPlanService


@dataclass(frozen=True, slots=True)
class CorrectionServiceGraph:
    """Hold completed-load correction services for either deployment."""

    stages: CorrectionAuthoringStageCoordinator
    origin_publisher: CorrectionOriginPublisher
    successors: CorrectionSuccessorService
    reviewer: CorrectionReviewOrchestrator
    executor: CorrectionExecutionService
    workflow: CorrectionWorkflowService


@dataclass(frozen=True, slots=True)
class PreparationServiceGraph:
    """Hold the target-independent preparation and final-review services."""

    quality: QualityService
    normalization: NormalizationService
    resolution: ResolutionService
    preparation: PreparationService
    preflight: PreflightService


def build_foundation_service_graph(
    repository: FoundationServiceRepository,
    workspace_state_repository: WorkspaceStateRepository,
    authorization: AuthorizationPolicy,
) -> FoundationServiceGraph:
    """Wire Project foundation use cases without choosing a deployment adapter."""

    workspace_access = WorkspaceAccessService(repository, authorization)
    workspace_states = WorkspaceStateService(
        workspace_state_repository,
        workspace_access,
    )
    migration_projects = MigrationProjectService(repository, authorization)
    data_versions = DataVersionService(repository, authorization)
    migration_runs = MigrationRunService(repository, authorization)
    migration_run_target_setup = MigrationRunTargetSetupService(
        repository,
        authorization,
    )
    migration_workspaces = MigrationWorkspaceService(repository, authorization)
    source_packages = DataVersionSourcePackageService(repository, authorization)
    project_authoring = MigrationProjectAuthoringService(
        migration_projects,
        data_versions,
        migration_runs,
        migration_workspaces,
        source_packages,
        workspace_states,
    )
    odoo_source_restarts = OdooSourceRestartService(
        projects=migration_projects,
        data_versions=data_versions,
        runs=migration_runs,
        workspaces=migration_workspaces,
        source_packages=source_packages,
        workspace_states=workspace_states,
    )
    return FoundationServiceGraph(
        workspace_access=workspace_access,
        workspace_states=workspace_states,
        migration_projects=migration_projects,
        data_versions=data_versions,
        migration_runs=migration_runs,
        migration_run_target_setup=migration_run_target_setup,
        migration_workspaces=migration_workspaces,
        source_packages=source_packages,
        project_authoring=project_authoring,
        odoo_source_restarts=odoo_source_restarts,
    )


def build_authoring_service_graph(
    *,
    foundation: FoundationServiceGraph,
    foundation_repository: AuthoringFoundationRepository,
    authorization: AuthorizationPolicy,
    workspaces: AuthoringWorkspacePorts,
    sources: AuthoringSourcePorts,
    mapping_sources: AuthoringMappingSourcePorts,
    schemas: AuthoringSchemaPorts,
    mappings: AuthoringMappingPorts,
    quality: RecipeAuthoringQualityRepository,
    derived_entities: AuthoringDerivedPorts,
    references: RecipeAuthoringReferenceRepository,
    recipe_parameters: RecipeAuthoringParameterRepository,
    supporting_lookups: MappingSupportingLookupReader,
    business_intents: BusinessIntentRepositoryPort,
    downstream_invalidator: MappingDownstreamInvalidator,
    recipe_applications: AuthoringRecipeApplicationState,
    row_inclusion: AuthoringRowInclusionPorts,
    matching_order: AuthoringMatchingOrderPorts,
    transformation_impacts: TransformationImpactRepository,
    recipes: RecipeRepository,
    artifacts: GovernedArtifactStores,
    capture_filters: OdooCaptureFilterStore,
    logical_source_values: LogicalSourceValues | None = None,
    exact_join_groups: ExactJoinGroupEvaluator | None = None,
) -> AuthoringServiceGraph:
    """Wire source, Recipe, and Match data without choosing storage adapters."""

    supporting_decisions = SupportingMappingGuard(workspaces, sources, schemas, derived_entities)
    recipe_compiler = RecipeCompiler(
        mapping_sources,
        mappings,
        schemas,
        quality,
        derived_entities,
        references,
        recipe_parameters,
        supporting_references=supporting_lookups,
        supporting_decisions=supporting_decisions,
    )
    recipe_service = RecipeService(recipes, authorization)
    recipe_publication = RecipePublicationService(
        recipes,
        recipe_compiler,
        authorization,
    )
    source_workspace = SourceWorkspaceService(
        workspaces,
        sources,
        foundation.workspace_access,
        artifacts,
        schemas=schemas,
        capture_filters=capture_filters,
    )
    source_projections = WorkspaceSourceProjectionService(
        foundation_repository,
        authorization,
    )
    data_version_source_projection = WorkspaceDataVersionSourceService(
        foundation.workspace_states,
        sources,
        foundation.data_versions,
        foundation.migration_workspaces,
        foundation.source_packages,
        source_projections,
        source_workspace,
    )
    categorical_coverage = CategoricalCoverageService(
        sources, artifacts, logical_source_values
    )
    numeric_precision_coverage = NumericPrecisionCoverageService(
        sources,
        artifacts,
    )
    schema_workspace = SchemaWorkspaceService(
        workspaces,
        sources,
        schemas,
        foundation.workspace_access,
        derived_entities=derived_entities,
    )
    mapping_workspace = MappingWorkspaceService(
        mapping_sources,
        schemas,
        mappings,
        foundation.workspace_access,
        categorical_coverage=categorical_coverage,
        numeric_precision_coverage=numeric_precision_coverage,
        supporting_lookups=supporting_lookups,
        downstream_invalidator=downstream_invalidator,
        recipe_applications=recipe_applications,
        row_inclusion_confirmations=row_inclusion,
        supporting_decisions=supporting_decisions,
    )
    transformation_impact_service = TransformationImpactService(
        workspaces,
        mappings,
        sources,
        derived_entities,
        transformation_impacts,
        artifacts,
        foundation.workspace_access,
    )
    row_inclusion_reviews = RowInclusionReviewService(
        transformation_impact_service,
        row_inclusion,
        foundation.workspace_access,
    )
    matching_order_service = MatchingOrderService(
        matching_order,
        foundation.workspace_access,
        categorical_coverage,
    )
    recipe_preparation = DerivedEntityWorkspaceService(
        sources,
        derived_entities,
        foundation.workspace_access,
        artifacts,
        exact_join_groups,
    )
    recipe_application = RecipeApplicationService(
        sources=mapping_sources,
        schemas=schemas,
        schema_workspace=schema_workspace,
        references=references,
        preparation=recipe_preparation,
        mappings=mapping_workspace,
        categorical=categorical_coverage,
        application_state=recipe_applications,
        row_inclusion_reviews=row_inclusion_reviews,
    )
    return AuthoringServiceGraph(
        business_intents=BusinessIntentService(
            workspaces=workspaces, sources=mapping_sources, schemas=schemas,
            mappings=mappings, repository=business_intents,
            authorization=foundation.workspace_access,
        ),
        recipe_compiler=recipe_compiler,
        recipes=recipe_service,
        recipe_publication=recipe_publication,
        source_workspace=source_workspace,
        source_projections=source_projections,
        data_version_source_projection=data_version_source_projection,
        categorical_coverage=categorical_coverage,
        numeric_precision_coverage=numeric_precision_coverage,
        schema_workspace=schema_workspace,
        mapping_workspace=mapping_workspace,
        transformation_impacts=transformation_impact_service,
        row_inclusion_reviews=row_inclusion_reviews,
        matching_order=matching_order_service,
        recipe_application=recipe_application,
    )


def build_run_service_graph(
    *,
    foundation: FoundationServiceGraph,
    authoring: AuthoringServiceGraph,
    planning_repository: RunPlanningRepository,
    test_run_repository: TestRunRepository,
    production_run_repository: ProductionRunRepository,
    cutover_plan_repository: RunCutoverPlanRepository,
    authorization: AuthorizationPolicy,
) -> RunServiceGraph:
    """Wire run planning and setup through application-owned persistence ports."""

    run_planning = MigrationRunPlanningService(
        projects=foundation.migration_projects,
        data_versions=foundation.data_versions,
        recipes=authoring.recipes,
        repository=planning_repository,
        test_run_values=test_run_repository,
        source_packages=foundation.source_packages,
        source_projections=authoring.source_projections,
        workspace_states=foundation.workspace_states,
        compiler=authoring.recipe_application,
        cutover_plans=cutover_plan_repository,
        authorization=authorization,
    )
    test_runs = TestRunSetupService(
        projects=foundation.migration_projects,
        data_versions=foundation.data_versions,
        runs=foundation.migration_runs,
        migration_workspaces=foundation.migration_workspaces,
        source_packages=foundation.source_packages,
        workspace_states=foundation.workspace_states,
        recipes=authoring.recipes,
        test_runs=test_run_repository,
        run_planning=run_planning,
        authorization=authorization,
    )
    production_runs = ProductionCutoverService(
        projects=foundation.migration_projects,
        data_versions=foundation.data_versions,
        runs=foundation.migration_runs,
        migration_workspaces=foundation.migration_workspaces,
        source_packages=foundation.source_packages,
        workspace_states=foundation.workspace_states,
        cutover_plans=cutover_plan_repository,
        production_runs=production_run_repository,
        run_planning=run_planning,
        authorization=authorization,
    )
    run_setups = RunSetupService(
        test_runs=test_runs,
        production_runs=production_runs,
        runs=foundation.migration_runs,
        recipes=authoring.recipes,
        authorization=authorization,
    )
    return RunServiceGraph(
        run_planning=run_planning,
        test_runs=test_runs,
        production_runs=production_runs,
        run_setups=run_setups,
    )


def build_execution_service_graph(
    *,
    workspaces: ExecutionWorkspacePorts,
    preflight: PreflightService,
    journal: ExecutionJournalPorts,
    reconciliation_results: ReconciliationResultRepository,
    sources: ExecutionSourcePorts,
    staging: FalloutStagingReader,
    schemas: ReconciliationSchemaReader,
    artifacts: GovernedArtifactStores,
    authorization: WorkspaceAuthorizationPolicy,
    provenance: OdooProvenanceService,
    current_read_credential_binding: ReadCredentialBindingProvider,
    fallout_workbook_renderer: FalloutWorkbookRenderer,
) -> ExecutionServiceGraph:
    """Wire reviewed loading and read-back without choosing deployment adapters."""

    execution = ExecutionService(
        workspaces,
        preflight,
        journal,
        authorization,
        require_remote_read_identity=True,
        require_remote_write_identity=True,
        current_read_credential_binding=current_read_credential_binding,
    )
    transfer_execution = TransferExecutionService(
        sources,
        artifacts,
        provenance,
        execution,
    )
    reconciliation = ReconciliationService(
        preflight,
        journal,
        reconciliation_results,
        authorization,
        ReconciliationEvidenceService(authorization, artifacts),
        schemas,
    )
    fallout_workbooks = FalloutWorkbookService(
        reconciliation,
        preflight,
        staging,
        sources,
        workspaces,
        artifacts,
        authorization,
        fallout_workbook_renderer,
    )
    return ExecutionServiceGraph(
        execution=execution,
        transfer_execution=transfer_execution,
        reconciliation=reconciliation,
        fallout_workbooks=fallout_workbooks,
    )


def build_cutover_service_graph(
    *,
    foundation: FoundationServiceGraph,
    preparation: PreparationServiceGraph,
    execution: ExecutionServiceGraph,
    run_planning: RunPlanningRepository,
    repository: RunCutoverPlanRepository,
    mappings: RecipeAuthoringMappingRepository,
    staging: PreflightStagingRepository,
    quality: PreflightQualityRepository,
    authorization: AuthorizationPolicy,
) -> CutoverServiceGraph:
    """Wire integrated Test qualification without choosing storage adapters."""

    qualification_evidence = WorkspaceIntegratedQualificationEvidenceReader(
        mappings=mappings,
        staging=staging,
        quality=quality,
        preflight=preparation.preflight,
        execution=execution.execution.journal,
        reconciliation=execution.reconciliation,
    )
    cutover_plans = CutoverPlanService(
        projects=foundation.migration_projects,
        data_versions=foundation.data_versions,
        run_planning=run_planning,
        repository=repository,
        evidence_reader=qualification_evidence,
        authorization=authorization,
    )
    return CutoverServiceGraph(
        qualification_evidence=qualification_evidence,
        cutover_plans=cutover_plans,
    )


def build_correction_service_graph(
    *,
    foundation: FoundationServiceGraph,
    authoring: AuthoringServiceGraph,
    preparation: PreparationServiceGraph,
    bindings: CorrectionBindingRepository,
    protected: CorrectionProtectedStore,
    mappings: RecipeAuthoringMappingRepository,
    sources: RecipeAuthoringSourceRepository,
    prepared: CorrectionPreparedEvidencePorts,
    preflight_records: CorrectionTargetSnapshotReader,
    execution: CorrectionExecutionPorts,
    reconciliations: CorrectionReconciliationPorts,
    artifacts: WorkspaceArtifactStore,
    target: CorrectionTargetReviewStage,
    review_pipeline_factory: CorrectionReviewPipelineFactory,
    authorization: AuthorizationPolicy,
) -> CorrectionServiceGraph:
    """Wire correction use cases without choosing secrets, Odoo, or storage."""

    stages = CorrectionAuthoringStageCoordinator(
        mapping=CurrentCorrectionMappingReviewStage(authoring.mapping_workspace),
        preparation=CurrentCorrectionPreparationReviewStage(
            preparation=preparation.preparation,
            sessions=prepared,
            mappings=mappings,
            sources=sources,
            artifacts=artifacts,
        ),
        quality=CurrentCorrectionQualityReviewStage(
            quality=preparation.quality,
            normalization=preparation.normalization,
        ),
        target=target,
    )
    origin_publisher = CorrectionOriginPublisher(bindings, protected)
    successors = CorrectionSuccessorService(
        bindings=bindings,
        projects=foundation.migration_projects,
        source_packages=foundation.source_packages.repository,
        runs=foundation.migration_runs,
        workspaces=foundation.migration_workspaces,
        workspace_states=foundation.workspace_states,
        source_projections=authoring.source_projections,
        target_setups=foundation.migration_run_target_setup,
        mapping_seeder=CorrectionMappingSeedService(
            schemas=authoring.schema_workspace,
            mappings=authoring.mapping_workspace,
        ),
    )
    reviewer = CorrectionReviewOrchestrator(
        bindings=bindings,
        protected=protected,
        pipeline=review_pipeline_factory(stages),
    )
    executor = CorrectionExecutionService(
        bindings=bindings,
        protected_store=protected,
        execution=execution,
        reconciliations=reconciliations,
        authorization=authorization,
    )
    workflow = CorrectionWorkflowService(
        bindings=bindings,
        protected=protected,
        origin_publisher=origin_publisher,
        successors=successors,
        reviewer=reviewer,
        executor=executor,
        runs=foundation.migration_runs,
        workspaces=foundation.migration_workspaces,
        mappings=mappings,
        preparations=prepared,
        preflight=preparation.preflight,
        preflight_repository=preflight_records,
        executions=execution,
        reconciliations=reconciliations,
    )
    return CorrectionServiceGraph(
        stages=stages,
        origin_publisher=origin_publisher,
        successors=successors,
        reviewer=reviewer,
        executor=executor,
        workflow=workflow,
    )


def build_preparation_service_graph(
    *,
    workspaces: PreparationWorkspacePorts,
    sources: PreparationSourcePorts,
    derived_entities: PreparationDerivedRepository,
    mappings: PreparationMappingPorts,
    staging: PreparationStagingPorts,
    sessions: PreparationSessionRepository,
    artifacts: GovernedArtifactStores,
    authorization: WorkspaceAuthorizationPolicy,
    quality_repository: PreparationQualityPorts,
    normalization_repository: PreparationNormalizationPorts,
    resolution_repository: PreparationResolutionPorts,
    preflight_repository: PreflightRepository,
    schemas: PreflightSchemaRepository,
    columnar_transformations: ColumnarTransformationPort,
    odoo_provenance: OdooProvenanceService,
    recipe_quality: RecipeQualitySeedRepository,
    grouped_exact_joins: GroupedExactJoinSourcePort | None = None,
    supporting_decisions: SupportingPreparationPolicy | None = None,
) -> PreparationServiceGraph:
    """Wire preparation and final review without choosing concrete adapters."""

    quality = QualityService(
        mappings,
        sources,
        quality_repository,
        recipe_quality=recipe_quality,
    )
    normalization = NormalizationService(
        normalization_repository,
        authorization,
    )
    resolution = ResolutionService(resolution_repository, staging)
    preparation = PreparationService(
        workspaces,
        sources,
        derived_entities,
        mappings,
        staging,
        sessions,
        artifacts,
        authorization,
        quality,
        normalization,
        columnar_transformations,
        resolution,
        odoo_provenance=odoo_provenance,
        schemas=schemas,
        grouped_exact_joins=grouped_exact_joins,
        supporting_decisions=supporting_decisions,
    )
    preflight = PreflightService(
        staging,
        quality_repository,
        normalization_repository,
        mappings,
        workspaces,
        sources,
        preflight_repository,
        artifacts,
        authorization,
        resolution_repository,
        schemas,
        odoo_provenance,
    )
    return PreparationServiceGraph(
        quality=quality,
        normalization=normalization,
        resolution=resolution,
        preparation=preparation,
        preflight=preflight,
    )
