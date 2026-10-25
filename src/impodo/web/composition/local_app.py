"""Assemble the local browser application and all concrete dependencies.

``create_local_app`` connects DuckDB repositories, filesystem artifacts,
application services, closed Odoo readers and writer, security middleware, and route
modules through :class:`impodo.web.context.WebContext`. Business rules belong
to the injected services and domain modules; this module owns construction and
local deployment choices only.

The load writer and post-write read-back reader remain separate from the
preflight connectors and are bound to the exact reviewed local-load preview.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_project_setup.py``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from impodo.adapters.duckdb.scoped_project_query import (
    LocalScopedMigrationProjectQuery,
)
from impodo.adapters.duckdb.scoped_workspace_access import (
    LocalScopedWorkspaceAccessQuery,
)
from impodo.adapters.jobs.inline import InlineJobDispatcher
from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader
from impodo.adapters.odoo.local_stack import LocalStackService
from impodo.application.data_version.inspection import SourceInspectionService
from impodo.application.data_version.intake import SourceIntakeService
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackageService,
    WorkspaceSourceProjectionService,
)
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.shared.build_contract import (
    PROCESS_BUILD_CONTRACT,
    ApplicationBuildContract,
)
from impodo.application.shared.jobs import JobDispatcher
from impodo.application.shared.secrets import SecretStore, SecretStoreError
from impodo.application.workspace.access import (
    WorkspaceAccessService,
)
from impodo.application.workspace.derived_entities import DerivedEntityWorkspaceService
from impodo.application.workspace.navigation import WorkspaceNavigationQueryService
from impodo.application.workspace.views import WorkspaceOwnerViewService
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.run.setup import MigrationRunTargetSetupService
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import (
    LOCAL_ACTOR,
    Actor,
    AuthorizationPolicy,
)
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateService,
)
from impodo.web.composition.incompatible_project_storage import (
    prepare_incompatible_project_storage,
)
from impodo.web.composition.preparation_job_manager import (
    PreparationJobManager,
)
from impodo.web.composition.target_readers import (
    _open_schema_read_session,
    _probe_read_identity,
    _read_destination_match,
    _read_model_catalog,
    _read_schema,
    _test_connection,
)
from impodo.web.composition.target_writers import (
    _probe_write_identity,
    _readback_reader,
    _write_executor,
)

from ...adapters.artifacts.fallout_workbook import build_fallout_workbook
from ...adapters.correction_review_pipeline import NativeCorrectionReviewPipeline
from ...adapters.duckdb.advanced_coverage_repository import AdvancedCoverageRepository
from ...adapters.duckdb.correction_repository import CorrectionRepository
from ...adapters.duckdb.data_version_source_repository import (
    DataVersionOwnedSourceRepository,
)
from ...adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from ...adapters.duckdb.execution_repository import ExecutionRepository
from ...adapters.duckdb.mapping_field_catalog_repository import (
    MappingFieldCatalogRepository,
)
from ...adapters.duckdb.mapping_repository import MappingRepository
from ...adapters.duckdb.matching_order_repository import MatchingOrderRepository
from ...adapters.duckdb.migration_run_planning_repository import (
    MigrationRunPlanningRepository,
)
from ...adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from ...adapters.duckdb.navigation_repository import WorkspaceNavigationRepository
from ...adapters.duckdb.normalization_repository import NormalizationRepository
from ...adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from ...adapters.duckdb.preflight_repository import PreflightRepository
from ...adapters.duckdb.preparation_recovery_repository import (
    PreparationRecoveryRepository,
)
from ...adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from ...adapters.duckdb.quality_repository import QualityRepository
from ...adapters.duckdb.recipe_compilation_repository import (
    RecipeCompilationRepository,
)
from ...adapters.duckdb.recipe_quality_seed_repository import (
    RecipeQualitySeedRepository,
)
from ...adapters.duckdb.reconciliation_repository import ReconciliationRepository
from ...adapters.duckdb.row_inclusion_review_repository import (
    RowInclusionReviewRepository,
)
from ...adapters.duckdb.run_aware_advanced_coverage_repository import (
    RunAwareAdvancedCoverageRepository,
)
from ...adapters.duckdb.run_aware_schema_repository import (
    RunAwareSchemaRepository,
)
from ...adapters.duckdb.schema_repository import SchemaRepository
from ...adapters.duckdb.staging_repository import StagingRepository
from ...adapters.duckdb.supporting_lookup_repository import (
    SupportingLookupRepository,
)
from ...adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from ...adapters.odoo_source_capture import Json2OdooSourceCapture
from ...adapters.polars_transformation import PolarsTransformationAdapter
from ...adapters.protected_correction_store import ProtectedCorrectionStore
from ...adapters.protected_destination_create_fields import (
    ProtectedDestinationCreateFieldStore,
)
from ...adapters.protected_odoo_capture_filters import ProtectedOdooCaptureFilterStore
from ...adapters.protected_odoo_comparison import ProtectedOdooComparisonCodec
from ...adapters.protected_odoo_provenance import ProtectedOdooProvenanceCodec
from ...adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from ...application.browser_queries import BrowserQueryService
from ...application.correction_execution import CorrectionExecutionService
from ...application.correction_jobs import CorrectionJobManager
from ...application.correction_orchestration import (
    CorrectionAuthoringStageCoordinator,
    CorrectionMappingSeedService,
    CorrectionOriginPublisher,
    CorrectionReviewOrchestrator,
    CorrectionSuccessorService,
    CorrectionTargetReviewEvidence,
)
from ...application.correction_stages import (
    CallbackCorrectionTargetReviewStage,
    CurrentCorrectionMappingReviewStage,
    CurrentCorrectionPreparationReviewStage,
    CurrentCorrectionQualityReviewStage,
)
from ...application.correction_workflow import CorrectionWorkflowService
from ...application.cutover_plan_service import (
    CutoverPlanService,
    WorkspaceIntegratedQualificationEvidenceReader,
)
from ...application.data_version.service import DataVersionService
from ...application.fallout_workbook_service import FalloutWorkbookService
from ...application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from ...application.odoo_capture_job_service import OdooCaptureJobManager
from ...application.odoo_capture_publication_service import (
    OdooCapturePublicationService,
)
from ...application.odoo_connection_service import OdooConnectionTestService
from ...application.odoo_provenance_service import OdooProvenanceService
from ...application.odoo_source_capture_service import OdooSourceCaptureService
from ...application.odoo_source_restart_service import OdooSourceRestartService
from ...application.preflight_jobs import PreflightJobManager
from ...application.preflight_service import PreflightService
from ...application.production_cutover_service import ProductionCutoverService
from ...application.project.service import MigrationProjectService
from ...application.recipe.service import RecipeService
from ...application.recipe_application_service import RecipeApplicationService
from ...application.recipe_compilation_service import RecipeCompiler
from ...application.recipe_publication_service import RecipePublicationService
from ...application.reconciliation_evidence_service import (
    ReconciliationEvidenceService,
)
from ...application.run.planning_service import (
    MigrationRunPlanningService,
)
from ...application.run.recipe_run_jobs import RecipeRunJobManager
from ...application.run.service import MigrationRunService
from ...application.run.setup_service import RunSetupService
from ...application.run.target_matches import RecipeTargetMatchService
from ...application.run.test_setup_service import TestRunSetupService
from ...application.schema_workspace_service import SchemaWorkspaceService
from ...application.source_workspace_service import SourceWorkspaceService
from ...application.supporting_lookup_service import SupportingLookupService
from ...application.transfer_execution_service import TransferExecutionService
from ...application.workspace.execution.load_jobs import LoadJobManager
from ...application.workspace.execution.reconciliation import ReconciliationService
from ...application.workspace.execution.service import ExecutionService
from ...application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from ...application.workspace.mapping.numeric_precision_coverage import (
    NumericPrecisionCoverageService,
)
from ...application.workspace.mapping.order_service import MatchingOrderService
from ...application.workspace.mapping.row_inclusion_review import (
    RowInclusionReviewService,
)
from ...application.workspace.mapping.service import MappingWorkspaceService
from ...application.workspace.mapping.transformation_impact import (
    TransformationImpactService,
)
from ...application.workspace.preparation.normalization_service import (
    NormalizationService,
)
from ...application.workspace.preparation.preparation_service import PreparationService
from ...application.workspace.preparation.quality_service import QualityService
from ...application.workspace.preparation.recovery import PreparationRecoveryService
from ...application.workspace.preparation.resolution_service import ResolutionService
from ...application.workspace.service import MigrationWorkspaceService
from ...application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from ...application.workspace_source_projection import (
    WorkspaceMappingSourceProjection,
)
from ..capability_builders import (
    build_foundation_capability,
    build_protected_run_capability,
)
from ..context import (
    BrowserReadinessReader,
    ConnectionTester,
    DestinationMatchingReader,
    ModelCatalogReader,
    OdooReadbackReaderFactory,
    OdooSourceCaptureFactory,
    OdooWriteExecutorFactory,
    ReadIdentityProbe,
    SchemaReader,
    SchemaReadSessionFactory,
    WebContext,
    WriteIdentityProbe,
)
from ..diagnostics import (
    LocalDiagnosticRecorder,
)
from ..remote_connection import RemoteConnectionStatusService
from ..request_identity import RequestIdentityResolver
from ..routers.lifecycle import build_lifecycle_router
from ..run_commands import publish_load_progress, publish_preparation_progress
from ..security import (
    LoopbackSecurityMiddleware,
)
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)
from .http_app import install_http_application
from .local_identity import (
    LOCAL_ORGANIZATION_ID,
    LocalRequestIdentityResolver,
)

DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS = 2.0
CLIENT_CLOSED_REQUEST_STATUS_CODE = 499


@dataclass(slots=True)
class LocalWebContext(WebContext):
    """Keep the implicit local actor out of the shared route context contract.

    Local integration fixtures and direct local automation still use this
    compatibility field while browser requests resolve identity through
    ``identity_resolver``. The server composition must use ``WebContext`` and
    cannot inherit this local-only shortcut.
    """

    actor: Actor


def create_local_app(
    project_root: str | Path,
    *,
    expected_host: str = "testserver",
    launch_token: str | None = None,
    session_secret: str | None = None,
    secret_store: SecretStore | None = None,
    connection_tester: ConnectionTester | None = None,
    read_identity_probe: ReadIdentityProbe | None = None,
    write_identity_probe: WriteIdentityProbe | None = None,
    schema_reader: SchemaReader | None = None,
    schema_read_session_factory: SchemaReadSessionFactory | None = None,
    model_catalog_reader: ModelCatalogReader | None = None,
    readiness_reader: BrowserReadinessReader | None = None,
    destination_match_reader: DestinationMatchingReader | None = None,
    source_capture_factory: OdooSourceCaptureFactory | None = None,
    write_executor_factory: OdooWriteExecutorFactory | None = None,
    readback_reader_factory: OdooReadbackReaderFactory | None = None,
    actor: Actor = LOCAL_ACTOR,
    authorization: AuthorizationPolicy | None = None,
    request_identity_resolver: RequestIdentityResolver | None = None,
    artifact_store: GovernedArtifactStores | None = None,
    job_dispatcher: JobDispatcher | None = None,
    local_stack_service: LocalStackService | None = None,
    local_odoo_reader: LocalOdooMetadataReader | None = None,
    preparation_jobs_enabled: bool = True,
    preflight_jobs_enabled: bool = False,
    odoo_capture_jobs_enabled: bool = True,
    recipe_run_jobs_enabled: bool = True,
    load_jobs_enabled: bool = True,
    duckdb_lock_wait_timeout_seconds: float = (
        DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS
    ),
    application_build_contract: ApplicationBuildContract = PROCESS_BUILD_CONTRACT,
    diagnostic_recorder: LocalDiagnosticRecorder | None = None,
) -> FastAPI:
    """Construct the loopback FastAPI application and its service graph.

    Production defaults use per-project DuckDB repositories, local artifact
    storage, the credential vault, inline jobs, closed read-only Odoo adapters,
    and the separate practical writer. Parameters expose the security, storage,
    job, reader, and writer seams so tests or another local composition can
    replace them without changing application/domain behavior.

    An injected diagnostic recorder adds privacy-safe lifecycle, request, and
    event-loop timing evidence. It does not change application decisions.

    The returned app keeps the assembled :class:`WebContext` in
    ``app.state.context`` and passes that same context to every router. This
    function opens no project and contacts no Odoo target while composing.
    """

    def local_source_capture_factory(
        workspace_state: WorkspaceState,
        api_key: str,
    ) -> Json2OdooSourceCapture:
        """Build the local JSON-2 capture adapter from one selected target."""

        if workspace_state.odoo_connection_mode is None:
            raise WorkspaceStateError(
                "Configure the Odoo target before source capture"
            )
        return Json2OdooSourceCapture(
            Json2Config(
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
                api_key=api_key,
                connection_mode=workspace_state.odoo_connection_mode.value,
            )
        )

    unavailable_projects = prepare_incompatible_project_storage(project_root)
    foundation = build_foundation_capability(
        project_root,
        artifact_store=artifact_store,
        lock_wait_timeout_seconds=duckdb_lock_wait_timeout_seconds,
    )
    foundation_database = foundation.foundation_database
    foundation_repository = foundation.foundation_repository
    correction_repository = CorrectionRepository(foundation_repository)
    database = foundation.workspace_database
    artifacts = foundation.artifacts
    workspace_state_repository = MigrationWorkspaceStateRepository(
        database,
        foundation_repository,
    )
    recipe_compilation_repository = RecipeCompilationRepository(database)
    derived_entity_repository = DerivedEntityRepository(database)
    workspace_mapping_sources = WorkspaceMappingSourceProjection(
        foundation_repository,
        derived_entity_repository,
    )
    source_repository = DataVersionOwnedSourceRepository(
        database,
        derived_entity_repository,
        workspace_mapping_sources,
        foundation=foundation_repository,
    )
    local_schema_repository = SchemaRepository(database)
    run_planning_repository = MigrationRunPlanningRepository(
        foundation_repository
    )
    schema_repository = RunAwareSchemaRepository(
        local_schema_repository,
        run_planning_repository,
    )
    mapping_repository = MappingRepository(database, workspace_mapping_sources)
    matching_order_repository = MatchingOrderRepository(
        database,
        workspace_mapping_sources,
    )
    supporting_lookup_repository = SupportingLookupRepository(database)
    mapping_field_catalog_repository = MappingFieldCatalogRepository(database)
    staging_repository = StagingRepository(
        database,
        artifacts,
        source_selections=source_repository,
    )
    preparation_session_repository = PreparationSessionRepository(
        database,
        artifacts,
    )
    local_advanced_coverage_repository = AdvancedCoverageRepository(database)
    advanced_coverage_repository = RunAwareAdvancedCoverageRepository(
        local_advanced_coverage_repository,
        run_planning_repository,
    )
    quality_repository = QualityRepository(database, workspace_state_repository)
    normalization_repository = NormalizationRepository(
        database,
        workspace_state_repository,
    )
    preflight_repository = PreflightRepository(database, workspace_state_repository)
    execution_repository = ExecutionRepository(database)
    reconciliation_repository = ReconciliationRepository(database)
    transformation_impact_repository = TransformationImpactRepository(database)
    row_inclusion_review_repository = RowInclusionReviewRepository(database)
    protected_runs = build_protected_run_capability(
        project_root,
        foundation_repository=foundation_repository,
        authorization=authorization,
        secret_store=secret_store,
    )
    resolved_authorization = protected_runs.authorization
    workspace_access = WorkspaceAccessService(
        foundation_repository,
        resolved_authorization,
    )
    request_workspace_access = LocalScopedWorkspaceAccessQuery(
        workspace_access,
        organization_id=LOCAL_ORGANIZATION_ID,
    )
    resolved_secret_store = protected_runs.secret_store
    cutover_plan_repository = protected_runs.cutover_plan_repository
    production_run_repository = protected_runs.production_run_repository
    test_run_repository = protected_runs.test_run_repository
    recipe_repository = protected_runs.recipe_repository
    odoo_provenance_repository = OdooProvenanceRepository(
        database,
        artifacts,
        protected_root=lambda workspace_id: (
            foundation_database.root
            / "artifacts"
            / "dv"
            / foundation_repository.get_migration_workspace(
                workspace_id
            ).data_version_id
            / "protected"
        ),
    )
    odoo_provenance_service = OdooProvenanceService(
        workspace_state_repository,
        source_repository,
        odoo_provenance_repository,
        resolved_secret_store,
        workspace_access,
        ProtectedOdooProvenanceCodec(),
        ProtectedOdooComparisonCodec(),
    )
    odoo_capture_filters = ProtectedOdooCaptureFilterStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )
    destination_create_fields = ProtectedDestinationCreateFieldStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )
    odoo_source_capture = OdooSourceCaptureService(
        workspace_state_repository,
        source_repository,
        schema_repository,
        workspace_access,
        capture_filters=odoo_capture_filters,
        relationship_scopes=source_repository,
    )
    odoo_capture_publication = OdooCapturePublicationService(
        odoo_source_capture,
        source_repository,
        odoo_provenance_service,
        odoo_provenance_repository,
        artifacts,
        workspace_access,
    )
    workspace_states = WorkspaceStateService(
        workspace_state_repository,
        workspace_access,
    )
    migration_projects = MigrationProjectService(
        foundation_repository,
        resolved_authorization,
    )
    data_versions = DataVersionService(
        foundation_repository,
        resolved_authorization,
    )
    migration_runs = MigrationRunService(
        foundation_repository,
        resolved_authorization,
    )
    migration_run_target_setup = MigrationRunTargetSetupService(
        foundation_repository,
        resolved_authorization,
    )
    migration_workspaces = MigrationWorkspaceService(
        foundation_repository,
        resolved_authorization,
    )
    source_packages = DataVersionSourcePackageService(
        foundation_repository,
        resolved_authorization,
    )
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
    recipe_compiler = RecipeCompiler(
        workspace_mapping_sources,
        mapping_repository,
        schema_repository,
        quality_repository,
        derived_entity_repository,
        advanced_coverage_repository,
        recipe_compilation_repository,
        supporting_references=supporting_lookup_repository,
    )
    recipes = RecipeService(
        recipe_repository,
        resolved_authorization,
    )
    recipe_publication = RecipePublicationService(
        recipe_repository,
        recipe_compiler,
        resolved_authorization,
    )
    sources = SourceWorkspaceService(
        workspace_state_repository,
        source_repository,
        workspace_access,
        artifacts,
        schemas=schema_repository,
        capture_filters=odoo_capture_filters,
    )
    data_version_source_projection = WorkspaceDataVersionSourceService(
        workspace_states,
        source_repository,
        data_versions,
        migration_workspaces,
        source_packages,
        WorkspaceSourceProjectionService(
            foundation_repository,
            resolved_authorization,
        ),
        sources,
    )
    categorical_coverage = CategoricalCoverageService(
        source_repository,
        artifacts,
    )
    numeric_precision_coverage = NumericPrecisionCoverageService(
        source_repository,
        artifacts,
    )
    schema_workspace = SchemaWorkspaceService(
        workspace_state_repository,
        source_repository,
        schema_repository,
        workspace_access,
    )
    recipe_application_state = RecipeQualitySeedRepository(database)
    mapping_workspace = MappingWorkspaceService(
        workspace_mapping_sources,
        schema_repository,
        mapping_repository,
        workspace_access,
        categorical_coverage=categorical_coverage,
        numeric_precision_coverage=numeric_precision_coverage,
        supporting_lookups=supporting_lookup_repository,
        downstream_invalidator=correction_repository,
        recipe_applications=recipe_application_state,
        row_inclusion_confirmations=row_inclusion_review_repository,
    )
    transformation_impacts = TransformationImpactService(
        workspace_state_repository,
        mapping_repository,
        source_repository,
        derived_entity_repository,
        transformation_impact_repository,
        artifacts,
        workspace_access,
    )
    row_inclusion_reviews = RowInclusionReviewService(
        transformation_impacts,
        row_inclusion_review_repository,
        workspace_access,
    )
    matching_order = MatchingOrderService(
        matching_order_repository,
        workspace_access,
        categorical_coverage,
    )
    recipe_application_service = RecipeApplicationService(
        sources=workspace_mapping_sources,
        schemas=schema_repository,
        schema_workspace=schema_workspace,
        references=advanced_coverage_repository,
        preparation=derived_entity_repository,
        mappings=mapping_workspace,
        categorical=categorical_coverage,
        application_state=recipe_application_state,
        row_inclusion_reviews=row_inclusion_reviews,
    )
    run_planning = MigrationRunPlanningService(
        projects=migration_projects,
        data_versions=data_versions,
        recipes=recipes,
        repository=run_planning_repository,
        test_run_values=test_run_repository,
        source_packages=source_packages,
        source_projections=WorkspaceSourceProjectionService(
            foundation_repository,
            resolved_authorization,
        ),
        workspace_states=workspace_states,
        compiler=recipe_application_service,
        cutover_plans=cutover_plan_repository,
        authorization=resolved_authorization,
    )
    test_runs = TestRunSetupService(
        projects=migration_projects,
        data_versions=data_versions,
        runs=migration_runs,
        migration_workspaces=migration_workspaces,
        source_packages=source_packages,
        workspace_states=workspace_states,
        recipes=recipes,
        test_runs=test_run_repository,
        run_planning=run_planning,
        authorization=resolved_authorization,
    )
    production_runs = ProductionCutoverService(
        projects=migration_projects,
        data_versions=data_versions,
        runs=migration_runs,
        migration_workspaces=migration_workspaces,
        source_packages=source_packages,
        workspace_states=workspace_states,
        cutover_plans=cutover_plan_repository,
        production_runs=production_run_repository,
        run_planning=run_planning,
        authorization=resolved_authorization,
    )
    run_setups = RunSetupService(test_runs=test_runs, production_runs=production_runs,
        runs=migration_runs, recipes=recipes, authorization=resolved_authorization)
    quality = QualityService(
        mapping_repository,
        source_repository,
        quality_repository,
        recipe_quality=recipe_application_state,
    )
    normalization = NormalizationService(
        normalization_repository,
        workspace_access,
    )
    resolution = ResolutionService(advanced_coverage_repository, staging_repository)
    preparation = PreparationService(
        workspace_state_repository,
        source_repository,
        derived_entity_repository,
        mapping_repository,
        staging_repository,
        preparation_session_repository,
        artifacts,
        workspace_access,
        quality,
        normalization,
        PolarsTransformationAdapter(),
        resolution,
        odoo_provenance=odoo_provenance_service,
        schemas=schema_repository,
    )
    preflight = PreflightService(
        staging_repository,
        quality_repository,
        normalization_repository,
        mapping_repository,
        workspace_state_repository,
        source_repository,
        preflight_repository,
        artifacts,
        workspace_access,
        advanced_coverage_repository,
        schema_repository,
        odoo_provenance_service,
    )

    def current_read_credential_binding(
        workspace_state: WorkspaceState,
        actor: Actor,
    ) -> str:
        credential_owner = context.target_credential_workspace(
            workspace_state.workspace_id,
            workspace_state=workspace_state,
            actor=actor,
        )
        if credential_owner.odoo_connection_mode is OdooConnectionMode.LOCAL:
            return local_read_credential_binding_hash(credential_owner)
        if credential_owner.odoo_connection_mode is not OdooConnectionMode.REMOTE:
            return ""
        try:
            credential = get_target_credential(
                resolved_secret_store,
                credential_owner,
                TargetCredentialRole.READ,
            )
        except SecretStoreError:
            return ""
        return credential.binding_hash if credential is not None else ""

    execution = ExecutionService(
        workspace_state_repository,
        preflight,
        execution_repository,
        workspace_access,
        require_remote_read_identity=True,
        require_remote_write_identity=True,
        current_read_credential_binding=current_read_credential_binding,
    )
    transfer_execution = TransferExecutionService(
        source_repository,
        artifacts,
        odoo_provenance_service,
        execution,
    )
    reconciliation = ReconciliationService(
        preflight,
        execution_repository,
        reconciliation_repository,
        workspace_access,
        ReconciliationEvidenceService(
            workspace_access,
            artifacts,
        ),
        schema_repository,
    )
    fallout_workbooks = FalloutWorkbookService(
        reconciliation,
        preflight,
        staging_repository,
        source_repository,
        workspace_state_repository,
        artifacts,
        workspace_access,
        build_fallout_workbook,
    )
    cutover_plans = CutoverPlanService(
        projects=migration_projects,
        data_versions=data_versions,
        run_planning=run_planning_repository,
        repository=cutover_plan_repository,
        evidence_reader=WorkspaceIntegratedQualificationEvidenceReader(
            mappings=mapping_repository,
            staging=staging_repository,
            quality=quality_repository,
            preflight=preflight,
            execution=execution_repository,
            reconciliation=reconciliation,
        ),
        authorization=resolved_authorization,
    )
    preparation_jobs = (
        PreparationJobManager(
            project_root,
            build_contract=application_build_contract,
            diagnostic_recorder=diagnostic_recorder,
        )
        if preparation_jobs_enabled
        else None
    )
    preflight_jobs = PreflightJobManager() if preflight_jobs_enabled else None
    odoo_capture_jobs = (
        OdooCaptureJobManager(
            odoo_capture_publication,
            accept_publication=lambda workspace_id, publication, actor: (
                data_version_source_projection.accept_odoo_capture(
                    workspace_id,
                    publication.source_selection,
                    publication.source_snapshots,
                    publication.manifests,
                    actor=actor,
                )
            ),
        )
        if odoo_capture_jobs_enabled
        else None
    )
    recipe_run_jobs = RecipeRunJobManager() if recipe_run_jobs_enabled else None
    load_jobs = LoadJobManager() if load_jobs_enabled else None
    resolved_connection_tester = connection_tester or _test_connection
    resolved_read_identity_probe = read_identity_probe or _probe_read_identity
    resolved_schema_reader = schema_reader or _read_schema
    if schema_read_session_factory is not None:
        resolved_schema_read_session_factory = schema_read_session_factory
    elif schema_reader is None and read_identity_probe is None:
        resolved_schema_read_session_factory = _open_schema_read_session
    else:
        # Existing injected readers predate request-local sessions and may
        # capture a fresh target fingerprint on every call.  Keep those
        # integrations on the established full-read path unless they opt in
        # with an explicit session factory.
        resolved_schema_read_session_factory = None
    resolved_readback_reader_factory = (
        readback_reader_factory or _readback_reader
    )
    protected_correction_store = ProtectedCorrectionStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )

    def correction_target_capability(
        manifest,
        mapping,
        _datasets,
        successor_workspace_id,
        _actor,
    ) -> CorrectionTargetReviewEvidence:
        """Bind one fresh reader to exact targets and governed relationships."""

        successor_state = workspace_state_repository.get(
            successor_workspace_id
        )
        completed_state = workspace_state_repository.get(
            manifest.completed_workspace_id
        )
        credential = get_target_credential(
            resolved_secret_store,
            successor_state,
            TargetCredentialRole.READ,
        ) or get_target_credential(
            resolved_secret_store,
            completed_state,
            TargetCredentialRole.READ,
        )
        if credential is None:
            raise SecretStoreError(
                "Enter an Odoo read API key before reviewing the correction"
            )
        previous = mapping_repository.get_mapping_revision(
            manifest.completed_workspace_id
        )
        if previous is None:
            raise CorrectionOriginError("Completed correction rules are missing")
        fields_by_model: dict[str, set[str]] = {}
        lookup_fields_by_model: dict[str, set[str]] = {}
        for definition in (previous.definition, mapping.definition):
            for dataset in definition.datasets:
                fields_by_model.setdefault(dataset.target_model, set()).update(
                    field.target_field for field in dataset.fields
                )
                fields_by_model[dataset.target_model].update(
                    relationship.target_field
                    for relationship in dataset.relationships
                )
                for relationship in dataset.relationships:
                    resolver = relationship.resolver
                    if resolver.model and resolver.key_mappings:
                        lookup_fields = {
                            item.target_field
                            for item in (
                                *resolver.key_mappings,
                                *resolver.scope_mappings,
                            )
                        }
                        fields_by_model.setdefault(
                            resolver.model,
                            set(),
                        ).update(lookup_fields)
                        lookup_fields_by_model.setdefault(
                            resolver.model,
                            set(),
                        ).update(lookup_fields)
        scope = OdooApiScope(
            preview_hash=content_hash(
                {
                    "correction_origin": manifest.manifest_hash,
                    "corrected_mapping": mapping.definition.content_hash,
                }
            ),
            models=tuple(
                OdooModelScope(
                    model=model,
                    read_fields=tuple(sorted(fields)),
                    lookup_fields=tuple(
                        sorted(lookup_fields_by_model.get(model, set()))
                    ),
                )
                for model, fields in sorted(fields_by_model.items())
                if fields
            ),
        )
        models = tuple(item.model for item in scope.models)
        identity = resolved_read_identity_probe(
            successor_state,
            credential.secret,
            models,
        )
        if (
            identity.target_hash != manifest.target_hash
            or not set(models).issubset(identity.readable_models)
        ):
            raise CorrectionOriginError(
                "Current Odoo read access does not match the completed load"
            )
        try:
            reviewed_at = datetime.fromisoformat(identity.observed_at)
        except ValueError as error:
            raise CorrectionOriginError(
                "Current Odoo read evidence has an invalid time"
            ) from error
        reader = resolved_readback_reader_factory(
            successor_state,
            credential.secret,
            scope,
        )
        return CorrectionTargetReviewEvidence(
            reader=reader,
            reader_scope_hash=scope.semantic_hash,
            read_credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            reviewed_at=reviewed_at,
        )

    correction_stages = CorrectionAuthoringStageCoordinator(
        mapping=CurrentCorrectionMappingReviewStage(mapping_workspace),
        preparation=CurrentCorrectionPreparationReviewStage(
            preparation=preparation,
            sessions=preparation_session_repository,
            mappings=mapping_repository,
            sources=source_repository,
            artifacts=artifacts,
        ),
        quality=CurrentCorrectionQualityReviewStage(
            quality=quality,
            normalization=normalization,
        ),
        target=CallbackCorrectionTargetReviewStage(
            correction_target_capability
        ),
    )
    correction_origin_publisher = CorrectionOriginPublisher(
        correction_repository,
        protected_correction_store,
    )
    correction_successors = CorrectionSuccessorService(
        bindings=correction_repository,
        runs=migration_runs,
        workspaces=migration_workspaces,
        workspace_states=workspace_states,
        source_projections=data_version_source_projection.projections,
        target_setups=migration_run_target_setup,
        mapping_seeder=CorrectionMappingSeedService(
            schemas=schema_workspace,
            mappings=mapping_workspace,
        ),
    )
    corrections = CorrectionWorkflowService(
        bindings=correction_repository,
        protected=protected_correction_store,
        origin_publisher=correction_origin_publisher,
        successors=correction_successors,
        reviewer=CorrectionReviewOrchestrator(
            bindings=correction_repository,
            protected=protected_correction_store,
            pipeline=NativeCorrectionReviewPipeline(correction_stages),
        ),
        executor=CorrectionExecutionService(
            bindings=correction_repository,
            protected_store=protected_correction_store,
            execution=execution_repository,
            reconciliations=reconciliation_repository,
            authorization=resolved_authorization,
        ),
        runs=migration_runs,
        workspaces=migration_workspaces,
        mappings=mapping_repository,
        preparations=preparation_session_repository,
        preflight=preflight,
        preflight_repository=preflight_repository,
        executions=execution_repository,
        reconciliations=reconciliation_repository,
    )
    correction_jobs = CorrectionJobManager()
    resolved_destination_match_reader = destination_match_reader
    if resolved_destination_match_reader is None and readiness_reader is not None:

        def injected_destination_match_reader(
            workspace_state,
            _api_key,
            metadata_requests,
            record_requests,
        ):
            return readiness_reader(
                workspace_state,
                metadata_requests,
                record_requests,
            )

        resolved_destination_match_reader = injected_destination_match_reader

    supporting_lookups = SupportingLookupService(supporting_lookup_repository, workspace_access)
    resolved_identity_resolver = (
        request_identity_resolver or LocalRequestIdentityResolver(actor)
    )
    context = LocalWebContext(
        queries=BrowserQueryService(
            workspace_state_repository,
            source_repository,
            derived_entity_repository,
            schema_repository,
            mapping_repository,
            mapping_field_catalog_repository,
            quality_repository,
            transformation_impact_repository,
            workspace_mapping_sources,
        ),
        project_queries=LocalScopedMigrationProjectQuery(
            foundation_repository,
            organization_id=LOCAL_ORGANIZATION_ID,
        ),
        identity_resolver=resolved_identity_resolver,
        unavailable_projects=unavailable_projects,
        workspace_access=workspace_access,
        workspace_views=WorkspaceOwnerViewService(
            foundation_repository,
            workspace_access,
        ),
        navigation=WorkspaceNavigationQueryService(
            WorkspaceNavigationRepository(database),
            execution,
            preparation_jobs=preparation_jobs,
            load_jobs=load_jobs,
        ),
        migration_projects=migration_projects,
        data_versions=data_versions,
        migration_runs=migration_runs,
        migration_run_target_setup=migration_run_target_setup,
        migration_workspaces=migration_workspaces,
        project_authoring=project_authoring,
        odoo_source_restarts=odoo_source_restarts,
        recipes=recipes,
        recipe_publication=recipe_publication,
        run_planning=run_planning,
        cutover_plans=cutover_plans,
        test_runs=test_runs,
        production_runs=production_runs,
        run_setups=run_setups,
        data_version_source_projection=data_version_source_projection,
        workspace_states=workspace_states,
        intake=SourceIntakeService(
            workspace_states,
            artifacts,
            workspace_access,
        ),
        inspections=SourceInspectionService(
            workspace_state_repository,
            source_repository,
            artifacts,
            workspace_access,
        ),
        sources=sources,
        derived_entities=DerivedEntityWorkspaceService(
            source_repository,
            derived_entity_repository,
            workspace_access,
            artifacts,
        ),
        schema_workspace=schema_workspace,
        mapping_workspace=mapping_workspace,
        matching_order=matching_order,
        supporting_lookups=supporting_lookups,
        recipe_target_matches=RecipeTargetMatchService(
            mapping_workspace, run_planning, supporting_lookups,
        ),
        recipe_run_jobs=recipe_run_jobs,
        categorical_coverage=categorical_coverage,
        preparation=preparation,
        preparation_recovery=PreparationRecoveryService(
            PreparationRecoveryRepository(database), mapping_workspace, workspace_state_repository,
        ),
        preparation_jobs=preparation_jobs,
        quality=quality,
        resolution=resolution,
        normalization=normalization,
        preflight=preflight,
        preflight_jobs=preflight_jobs,
        execution=execution,
        transfer_execution=transfer_execution,
        load_jobs=load_jobs,
        reconciliation=reconciliation,
        fallout_workbooks=fallout_workbooks,
        corrections=corrections,
        correction_jobs=correction_jobs,
        transformation_impacts=transformation_impacts,
        row_inclusion_reviews=row_inclusion_reviews,
        odoo_source_capture=odoo_source_capture,
        odoo_capture_publication=odoo_capture_publication,
        odoo_capture_jobs=odoo_capture_jobs,
        odoo_provenance=odoo_provenance_service,
        destination_create_fields=destination_create_fields,
        artifacts=artifacts,
        actor=actor,
        authorization=resolved_authorization,
        jobs=job_dispatcher or InlineJobDispatcher(),
        secret_store=resolved_secret_store,
        launch_token=launch_token or secrets.token_urlsafe(32),
        connection_tester=resolved_connection_tester,
        read_identity_probe=resolved_read_identity_probe,
        write_identity_probe=write_identity_probe or _probe_write_identity,
        schema_reader=resolved_schema_reader,
        schema_read_session_factory=resolved_schema_read_session_factory,
        model_catalog_reader=model_catalog_reader or _read_model_catalog,
        readiness_reader=readiness_reader,
        destination_match_reader=(
            resolved_destination_match_reader or _read_destination_match
        ),
        source_capture_factory=source_capture_factory or local_source_capture_factory,
        write_executor_factory=write_executor_factory or _write_executor,
        readback_reader_factory=resolved_readback_reader_factory,
        local_stack=local_stack_service or LocalStackService(),
        local_odoo_reader=local_odoo_reader or LocalOdooMetadataReader(),
        odoo_connection_tests=OdooConnectionTestService(
            resolved_connection_tester,
            resolved_read_identity_probe,
        ),
        remote_connections=RemoteConnectionStatusService(),
    )
    if preparation_jobs is not None:
        preparation_jobs.set_result_reader(
            lambda job, actor: context.preparation_recovery.current(
                job.workspace_id, job.workspace.mapping_content_hash, actor=actor,
            )
        )
        preparation_jobs.set_status_listener(
            lambda job, actor: publish_preparation_progress(
                context, job, actor=actor,
            )
        )
    if load_jobs is not None:
        load_jobs.set_status_listener(
            lambda job, actor: publish_load_progress(context, job, actor=actor)
        )

    def install_local_transport_security(app: FastAPI) -> None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret or secrets.token_urlsafe(48),
            session_cookie="impodo_session",
            # The signing secret scopes this cookie to one launcher lifetime.
            max_age=None,
            same_site="strict",
            https_only=False,
        )
        app.add_middleware(
            LoopbackSecurityMiddleware,
            expected_host=expected_host,
        )

    return install_http_application(
        context,
        application_build_contract=application_build_contract,
        diagnostic_recorder=diagnostic_recorder,
        request_workspace_access=request_workspace_access,
        identity_resolver=resolved_identity_resolver,
        lifecycle_router=build_lifecycle_router(context.lifecycle_routes()),
        install_transport_security=install_local_transport_security,
    )
