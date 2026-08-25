"""Assemble the local browser application and all concrete dependencies.

Migration stages: cross-cutting A–K. Layer: composition root.

``create_local_app`` connects DuckDB repositories, filesystem artifacts,
application services, closed Odoo readers and writer, security middleware, and route
modules through :class:`impodo.web.context.WebContext`. Business rules belong
to the injected services and domain modules; this module owns construction and
local deployment choices only.

The Stage-J writer and Stage-K read-back reader remain separate from the
preflight connectors and are bound to the exact reviewed local-load preview.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..access import (
    Actor,
    AuthorizationError,
    AuthorizationPolicy,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from ..application.browser_queries import BrowserQueryService
from ..application.mapping_workspace_service import MappingWorkspaceService
from ..application.categorical_coverage_service import CategoricalCoverageService
from ..application.normalization_service import NormalizationService
from ..application.odoo_capture_publication_service import OdooCapturePublicationService
from ..application.odoo_capture_job_service import OdooCaptureJobManager
from ..application.odoo_provenance_service import OdooProvenanceService
from ..application.odoo_source_capture_service import OdooSourceCaptureService
from ..application.preflight_service import PreflightService
from ..application.execution_service import ExecutionService
from ..application.load_job_service import LoadJobManager
from ..application.reconciliation_service import ReconciliationService
from ..application.recipe_compilation_service import RecipeCompiler
from ..application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from ..build_contract import ApplicationBuildContract, PROCESS_BUILD_CONTRACT
from ..application.migration_run_planning_service import (
    MigrationRunPlanningService,
)
from ..application.cutover_plan_service import (
    CutoverPlanService,
    WorkspaceIntegratedQualificationEvidenceReader,
)
from ..application.production_cutover_service import ProductionCutoverService
from ..application.test_run_setup_service import TestRunSetupService
from ..application.recipe_application_service import RecipeApplicationService
from ..application.recipe_publication_service import RecipePublicationService
from ..application.workspace_source_projection import (
    WorkspaceMappingSourceProjection,
)
from ..application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from ..application.preparation_service import PreparationService
from ..application.preparation_job_service import PreparationJobManager
from ..application.quality_service import QualityService
from ..application.resolution_service import ResolutionService
from ..application.schema_workspace_service import SchemaWorkspaceService
from ..application.source_workspace_service import SourceWorkspaceService
from ..application.supporting_lookup_service import SupportingLookupService
from ..application.transformation_impact_service import TransformationImpactService
from ..artifacts import GovernedArtifactStores, LocalArtifactStore
from ..derived_entities import DerivedEntityWorkspaceService
from ..intake import SourceIntakeService
from ..inspection import SourceInspectionService
from ..incompatible_project_storage import prepare_incompatible_project_storage
from ..jobs import InlineJobDispatcher, JobDispatcher
from ..local_odoo_reader import LocalOdooMetadataReader
from ..local_stack import LocalStackService
from ..adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from ..adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from ..adapters.duckdb.migration_workspace_engine_database import (
    MigrationWorkspaceEngineDatabase,
)
from ..adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from ..adapters.duckdb.recipe_repository import RecipeRepository
from ..adapters.duckdb.migration_run_planning_repository import (
    MigrationRunPlanningRepository,
)
from ..adapters.duckdb.cutover_plan_repository import CutoverPlanRepository
from ..adapters.duckdb.production_run_repository import ProductionRunRepository
from ..adapters.duckdb.test_run_repository import TestRunRepository
from ..adapters.duckdb.run_aware_schema_repository import (
    RunAwareSchemaRepository,
)
from ..adapters.duckdb.run_aware_advanced_coverage_repository import (
    RunAwareAdvancedCoverageRepository,
)
from ..adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from ..adapters.duckdb.mapping_repository import MappingRepository
from ..adapters.duckdb.mapping_field_catalog_repository import (
    MappingFieldCatalogRepository,
)
from ..adapters.duckdb.normalization_repository import NormalizationRepository
from ..adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from ..adapters.duckdb.preflight_repository import PreflightRepository
from ..adapters.duckdb.execution_repository import ExecutionRepository
from ..adapters.duckdb.reconciliation_repository import ReconciliationRepository
from ..adapters.duckdb.recipe_compilation_repository import (
    RecipeCompilationRepository,
)
from ..adapters.duckdb.recipe_quality_seed_repository import (
    RecipeQualitySeedRepository,
)
from ..adapters.duckdb.quality_repository import QualityRepository
from ..adapters.duckdb.schema_repository import SchemaRepository
from ..adapters.duckdb.data_version_source_repository import (
    DataVersionOwnedSourceRepository,
)
from ..adapters.duckdb.supporting_lookup_repository import (
    SupportingLookupRepository,
)
from ..adapters.duckdb.staging_repository import StagingRepository
from ..adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from ..adapters.duckdb.advanced_coverage_repository import AdvancedCoverageRepository
from ..adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from ..adapters.protected_recipe_store import ProtectedRecipeStore
from ..adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from ..workspace_state import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStateCompatibilityError,
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
    SourceMode,
)
from ..data_version_sources import (
    DataVersionSourcePackageService,
    WorkspaceSourceProjectionService,
)
from ..data_versions import DataVersionService
from ..migration_projects import MigrationProjectService
from ..migration_runs import MigrationRunService
from ..migration_run_setup import MigrationRunTargetSetupService
from ..migration_workspaces import MigrationWorkspaceService
from ..recipes import RecipeError, RecipeService
from ..application.odoo_connection_service import OdooConnectionTestService
from ..migration_foundation import (
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
)
from ..workspace_access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
)
from ..workspace_views import WorkspaceOwnerViewService
from ..secrets import CredentialVault, SecretStore, SecretStoreError
from .context import (
    BrowserReadinessReader,
    ConnectionTester,
    ModelCatalogReader,
    OdooWriteExecutorFactory,
    OdooReadbackReaderFactory,
    OdooSourceCaptureFactory,
    ReadIdentityProbe,
    SchemaReader,
    WriteIdentityProbe,
    WebContext,
)
from .presenters.common import _render
from .target_readers import (
    _read_model_catalog,
    _read_schema,
    _probe_read_identity,
    _test_connection,
    _source_capture_reader,
)
from .target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)
from .target_writers import _probe_write_identity, _readback_reader, _write_executor
from .routers.derived_entities import build_derived_entities_router
from .routers.concepts import build_concepts_router
from .routers.lifecycle import build_lifecycle_router
from .routers.mapping import build_mapping_router
from .routers.normalization import build_normalization_router
from .routers.preflight import build_preflight_router
from .routers.execution import build_execution_router
from .routers.preparation import build_preparation_router
from .routers.integrated_runs import build_integrated_runs_router
from .routers.cutover_plans import build_cutover_plans_router
from .routers.production_runs import build_production_runs_router
from .routers.migration_projects import build_migration_projects_router
from .routers.workspace_setup import build_workspace_setup_router
from .routers.quality import build_quality_router
from .routers.resolution import build_resolution_router
from .routers.schema import build_schema_router
from .routers.sources import build_sources_router
from .routers.summary import build_summary_router
from .routers.target import build_target_router
from .remote_connection import RemoteConnectionStatusService
from .run_review import publish_load_progress, publish_preparation_progress
from .security import (
    BuildConsistencyMiddleware,
    LoopbackSecurityMiddleware,
    WorkspaceAccessMiddleware,
)
from .workspace_journeys import (
    WorkspaceJourney,
    enforce_workspace_journey,
    workspace_route_is_allowed,
)


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
    model_catalog_reader: ModelCatalogReader | None = None,
    readiness_reader: BrowserReadinessReader | None = None,
    source_capture_factory: OdooSourceCaptureFactory | None = None,
    write_executor_factory: OdooWriteExecutorFactory | None = None,
    readback_reader_factory: OdooReadbackReaderFactory | None = None,
    actor: Actor = LOCAL_ACTOR,
    authorization: AuthorizationPolicy | None = None,
    artifact_store: GovernedArtifactStores | None = None,
    job_dispatcher: JobDispatcher | None = None,
    local_stack_service: LocalStackService | None = None,
    local_odoo_reader: LocalOdooMetadataReader | None = None,
    preparation_jobs_enabled: bool = True,
    odoo_capture_jobs_enabled: bool = True,
    load_jobs_enabled: bool = True,
    duckdb_lock_wait_timeout_seconds: float = 0.0,
    application_build_contract: ApplicationBuildContract = PROCESS_BUILD_CONTRACT,
) -> FastAPI:
    """Construct the loopback FastAPI application for migration Stages A–K.

    Production defaults use per-project DuckDB repositories, local artifact
    storage, the credential vault, inline jobs, closed read-only Odoo adapters,
    and the separate practical writer. Parameters expose the security, storage,
    job, reader, and writer seams so tests or another local composition can
    replace them without changing application/domain behavior.

    The returned app keeps the assembled :class:`WebContext` in
    ``app.state.context`` and passes that same context to every router. This
    function opens no project and contacts no Odoo target while composing.
    """

    unavailable_projects = prepare_incompatible_project_storage(project_root)
    foundation_database = MigrationFoundationDatabase(
        project_root,
        lock_wait_timeout_seconds=duckdb_lock_wait_timeout_seconds,
    )
    foundation_repository = MigrationFoundationRepository(foundation_database)
    database = MigrationWorkspaceEngineDatabase(
        foundation_database,
        lock_wait_timeout_seconds=duckdb_lock_wait_timeout_seconds,
    )
    artifacts = artifact_store or LocalArtifactStore(
        Path(project_root) / "artifacts"
    )
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
    supporting_lookup_repository = SupportingLookupRepository(database)
    mapping_field_catalog_repository = MappingFieldCatalogRepository(database)
    staging_repository = StagingRepository(database, artifacts)
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
    resolved_authorization = authorization or CapabilityAuthorizationPolicy()
    workspace_access = WorkspaceAccessService(
        foundation_repository,
        resolved_authorization,
    )
    resolved_secret_store = secret_store or CredentialVault()
    protected_recipe_store = ProtectedRecipeStore(
        project_root,
        resolved_secret_store,
    )
    cutover_plan_repository = CutoverPlanRepository(
        foundation_repository,
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store),
    )
    production_run_repository = ProductionRunRepository(foundation_repository)
    test_run_repository = TestRunRepository(foundation_repository)
    recipe_repository = RecipeRepository(
        foundation_repository,
        protected_recipe_store,
    )
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
    )
    odoo_capture_publication = OdooCapturePublicationService(
        OdooSourceCaptureService(
            workspace_state_repository,
            source_repository,
            schema_repository,
            workspace_access,
        ),
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
    recipe_compiler = RecipeCompiler(
        workspace_mapping_sources,
        mapping_repository,
        schema_repository,
        quality_repository,
        derived_entity_repository,
        advanced_coverage_repository,
        recipe_compilation_repository,
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
    )
    categorical_coverage = CategoricalCoverageService(
        source_repository,
        artifacts,
    )
    schema_workspace = SchemaWorkspaceService(
        workspace_state_repository,
        source_repository,
        schema_repository,
        workspace_access,
    )
    mapping_workspace = MappingWorkspaceService(
        workspace_mapping_sources,
        schema_repository,
        mapping_repository,
        workspace_access,
        categorical_coverage=categorical_coverage,
        supporting_lookups=supporting_lookup_repository,
    )
    recipe_application_service = RecipeApplicationService(
        sources=workspace_mapping_sources,
        schemas=schema_repository,
        schema_workspace=schema_workspace,
        references=advanced_coverage_repository,
        preparation=derived_entity_repository,
        mappings=mapping_workspace,
        categorical=categorical_coverage,
        application_state=RecipeQualitySeedRepository(database),
    )
    run_planning = MigrationRunPlanningService(
        projects=migration_projects,
        data_versions=data_versions,
        recipes=recipes,
        repository=run_planning_repository,
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
    quality = QualityService(
        mapping_repository,
        source_repository,
        quality_repository,
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
        resolution,
        odoo_provenance=odoo_provenance_service,
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

    def current_read_credential_binding(workspace_state: WorkspaceState) -> str:
        test_credential_owner = test_runs.credential_workspace(
            workspace_state.workspace_id,
            actor=actor,
        )
        credential_owner = production_runs.credential_workspace(
            test_credential_owner.workspace_id,
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
    reconciliation = ReconciliationService(
        preflight,
        execution_repository,
        reconciliation_repository,
        workspace_access,
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
        )
        if preparation_jobs_enabled
        else None
    )
    odoo_capture_jobs = (
        OdooCaptureJobManager(
            odoo_capture_publication,
            accept_publication=lambda workspace_id, publication, actor: (
                data_version_source_projection.accept_odoo_capture(
                    workspace_id,
                    publication.source_selection,
                    publication.source_snapshot,
                    publication.manifest,
                    actor=actor,
                )
            ),
        )
        if odoo_capture_jobs_enabled
        else None
    )
    load_jobs = LoadJobManager() if load_jobs_enabled else None
    resolved_connection_tester = connection_tester or _test_connection
    resolved_read_identity_probe = read_identity_probe or _probe_read_identity
    context = WebContext(
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
        unavailable_projects=unavailable_projects,
        workspace_access=workspace_access,
        workspace_views=WorkspaceOwnerViewService(
            foundation_repository,
            workspace_access,
        ),
        migration_projects=migration_projects,
        data_versions=data_versions,
        migration_runs=migration_runs,
        migration_run_target_setup=migration_run_target_setup,
        migration_workspaces=migration_workspaces,
        project_authoring=project_authoring,
        recipes=recipes,
        recipe_publication=recipe_publication,
        run_planning=run_planning,
        cutover_plans=cutover_plans,
        test_runs=test_runs,
        production_runs=production_runs,
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
        sources=SourceWorkspaceService(
            workspace_state_repository,
            source_repository,
            workspace_access,
            artifacts,
            schemas=schema_repository,
        ),
        derived_entities=DerivedEntityWorkspaceService(
            source_repository,
            derived_entity_repository,
            workspace_access,
        ),
        schema_workspace=schema_workspace,
        mapping_workspace=mapping_workspace,
        supporting_lookups=SupportingLookupService(
            supporting_lookup_repository,
            workspace_access,
        ),
        categorical_coverage=categorical_coverage,
        preparation=preparation,
        preparation_jobs=preparation_jobs,
        quality=quality,
        resolution=resolution,
        normalization=normalization,
        preflight=preflight,
        execution=execution,
        load_jobs=load_jobs,
        reconciliation=reconciliation,
        transformation_impacts=TransformationImpactService(
            workspace_state_repository,
            mapping_repository,
            source_repository,
            derived_entity_repository,
            transformation_impact_repository,
            artifacts,
            workspace_access,
        ),
        odoo_capture_publication=odoo_capture_publication,
        odoo_capture_jobs=odoo_capture_jobs,
        odoo_provenance=odoo_provenance_service,
        artifacts=artifacts,
        actor=actor,
        authorization=resolved_authorization,
        jobs=job_dispatcher or InlineJobDispatcher(),
        secret_store=resolved_secret_store,
        launch_token=launch_token or secrets.token_urlsafe(32),
        connection_tester=resolved_connection_tester,
        read_identity_probe=resolved_read_identity_probe,
        write_identity_probe=write_identity_probe or _probe_write_identity,
        schema_reader=schema_reader or _read_schema,
        model_catalog_reader=model_catalog_reader or _read_model_catalog,
        readiness_reader=readiness_reader,
        source_capture_factory=source_capture_factory or _source_capture_reader,
        write_executor_factory=write_executor_factory or _write_executor,
        readback_reader_factory=readback_reader_factory or _readback_reader,
        local_stack=local_stack_service or LocalStackService(),
        local_odoo_reader=local_odoo_reader or LocalOdooMetadataReader(),
        odoo_connection_tests=OdooConnectionTestService(
            resolved_connection_tester,
            resolved_read_identity_probe,
        ),
        remote_connections=RemoteConnectionStatusService(),
    )
    if preparation_jobs is not None:
        preparation_jobs.set_status_listener(
            lambda job: publish_preparation_progress(context, job)
        )
    if load_jobs is not None:
        load_jobs.set_status_listener(
            lambda job: publish_load_progress(context, job)
        )

    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=package_dir / "templates")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if context.preparation_jobs is not None:
                context.preparation_jobs.shutdown()
            if context.odoo_capture_jobs is not None:
                context.odoo_capture_jobs.shutdown()
            if context.load_jobs is not None:
                context.load_jobs.shutdown()

    app = FastAPI(
        title="Impodo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.build_contract = application_build_contract
    app.state.server = None
    app.state.templates = templates
    app.mount(
        "/static",
        StaticFiles(directory=package_dir / "static"),
        name="static",
    )

    def trusted_job_context(
        path: str,
        workspace_id: str,
    ) -> WorkspaceAccessContext | None:
        """Reuse an already verified job packet without reopening the registry."""

        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[:2] != ["workspaces", workspace_id]:
            return None
        try:
            if (
                parts[2] == "preparation"
                and context.preparation_jobs is not None
            ):
                job = context.preparation_jobs.get(workspace_id, parts[3])
                return WorkspaceAccessContext(
                    project_id=job.workspace.project_id,
                    workspace_id=job.workspace.workspace_id,
                    data_version_id=job.workspace.data_version_id,
                    migration_run_id=job.workspace.migration_run_id,
                    recipe_application_id=job.workspace.recipe_application_id,
                    run_purpose=job.workspace.migration_run_purpose.value,
                )
            if (
                len(parts) >= 5
                and parts[2:4] == ["load", "progress"]
                and context.load_jobs is not None
            ):
                return context.load_jobs.get(workspace_id, parts[4]).access_context
            if (
                len(parts) >= 5
                and parts[2:4] == ["sources", "odoo-capture"]
                and context.odoo_capture_jobs is not None
            ):
                return context.odoo_capture_jobs.get(
                    workspace_id,
                    parts[4],
                ).access_context
        except LookupError:
            return None
        return None

    def workspace_route_policy(request, access_context):
        """Keep Recipe-run workspaces inside their owning run journey."""

        if (
            access_context.recipe_application_id is not None
            and workspace_route_is_allowed(
                WorkspaceJourney.RECIPE_APPLICATION,
                request.url.path,
                access_context.workspace_id,
            )
        ):
            # Progress/status requests keep using their verified job packet and
            # do not reopen the registry merely to confirm an allowed route.
            return None
        if access_context.run_purpose is None:
            raise MigrationIdentifierConfusionError(
                "Workspace route ownership is missing its MigrationRun purpose"
            )
        return enforce_workspace_journey(
            request,
            access_context,
            access_context.run_purpose,
        )

    app.add_middleware(
        WorkspaceAccessMiddleware,
        access=context.workspace_access,
        actor=lambda: context.actor,
        trusted_context_resolver=trusted_job_context,
        route_policy=workspace_route_policy,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret or secrets.token_urlsafe(48),
        session_cookie="impodo_session",
        max_age=30 * 60,
        same_site="strict",
        https_only=False,
    )
    app.add_middleware(
        BuildConsistencyMiddleware,
        expected=application_build_contract,
    )
    app.add_middleware(
        LoopbackSecurityMiddleware,
        expected_host=expected_host,
    )

    @app.exception_handler(WorkspaceStateNotFoundError)
    async def project_not_found(_request: Request, _error: WorkspaceStateNotFoundError):
        return HTMLResponse("Project not found", status_code=404)

    @app.exception_handler(MigrationNotFoundError)
    async def migration_not_found(_request: Request, _error: MigrationNotFoundError):
        return HTMLResponse("Migration record not found", status_code=404)

    @app.exception_handler(MigrationIdentifierConfusionError)
    async def workspace_identity_mismatch(
        request: Request,
        _error: MigrationIdentifierConfusionError,
    ):
        message = (
            "Workspace not found"
            if request.url.path.startswith("/workspaces/")
            else "Migration record not found"
        )
        return HTMLResponse(message, status_code=404)

    @app.exception_handler(AuthorizationError)
    async def command_not_authorized(_request: Request, _error: AuthorizationError):
        return HTMLResponse("Not authorized", status_code=403)

    @app.exception_handler(RecipeError)
    async def recipe_error(_request: Request, error: RecipeError):
        return HTMLResponse(str(error), status_code=422)

    @app.exception_handler(WorkspaceStateCompatibilityError)
    async def project_incompatible(
        request: Request,
        error: WorkspaceStateCompatibilityError,
    ):
        return _render(
            request,
            "project_list.html",
            projects=context.migration_projects.list(actor=context.actor),
            unavailable_projects=context.unavailable_projects,
            error=str(error),
            status_code=409,
        )

    for router in (
        build_lifecycle_router(context),
        build_concepts_router(),
        build_migration_projects_router(context),
        build_integrated_runs_router(context),
        build_cutover_plans_router(context),
        build_production_runs_router(context),
        build_workspace_setup_router(context),
        build_target_router(context),
        build_sources_router(context),
        build_schema_router(context),
        build_derived_entities_router(context),
        build_mapping_router(context),
        build_quality_router(context),
        build_preparation_router(context),
        build_resolution_router(context),
        build_normalization_router(context),
        build_summary_router(context),
        build_preflight_router(context),
        build_execution_router(context),
    ):
        app.include_router(router)

    return app
