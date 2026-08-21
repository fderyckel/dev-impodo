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
from ..application.reconciliation_service import ReconciliationService
from ..application.recipe_service import RecipeService
from ..application.recipe_authoring_service import RecipeAuthoringService
from ..application.recipe_application_service import RecipeApplicationService
from ..application.recipe_qualification_service import RecipeQualificationService
from ..application.preparation_service import PreparationService
from ..application.preparation_job_service import PreparationJobManager
from ..application.quality_service import QualityService
from ..application.resolution_service import ResolutionService
from ..application.schema_workspace_service import SchemaWorkspaceService
from ..application.source_workspace_service import SourceWorkspaceService
from ..application.transformation_impact_service import TransformationImpactService
from ..artifacts import ArtifactStore, LocalArtifactStore
from ..derived_entities import DerivedEntityWorkspaceService
from ..intake import SourceIntakeService
from ..inspection import SourceInspectionService
from ..jobs import InlineJobDispatcher, JobDispatcher
from ..local_odoo_reader import LocalOdooMetadataReader
from ..local_stack import LocalStackService
from ..adapters.duckdb.database import DuckDbDatabase
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
from ..adapters.duckdb.project_repository import ProjectRepository
from ..adapters.duckdb.recipe_repository import RecipeRepository
from ..adapters.duckdb.recipe_authoring_repository import RecipeAuthoringRepository
from ..adapters.duckdb.recipe_application_repository import (
    RecipeApplicationRepository,
)
from ..adapters.duckdb.quality_repository import QualityRepository
from ..adapters.duckdb.schema_repository import SchemaRepository
from ..adapters.duckdb.source_repository import SourceRepository
from ..adapters.duckdb.staging_repository import StagingRepository
from ..adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from ..adapters.duckdb.advanced_coverage_repository import AdvancedCoverageRepository
from ..adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from ..adapters.protected_recipe_store import ProtectedRecipeStore
from ..projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectCompatibilityError,
    ProjectNotFoundError,
    ProjectService,
    SourceMode,
)
from ..application.odoo_connection_service import OdooConnectionTestService
from ..recipes import RecipeNotFoundError
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
from .routers.lifecycle import build_lifecycle_router
from .routers.mapping import build_mapping_router
from .routers.normalization import build_normalization_router
from .routers.preflight import build_preflight_router
from .routers.execution import build_execution_router
from .routers.preparation import build_preparation_router
from .routers.projects import build_projects_router
from .routers.quality import build_quality_router
from .routers.resolution import build_resolution_router
from .routers.schema import build_schema_router
from .routers.sources import build_sources_router
from .routers.summary import build_summary_router
from .routers.target import build_target_router
from .remote_connection import RemoteConnectionStatusService
from .security import LoopbackSecurityMiddleware


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
    artifact_store: ArtifactStore | None = None,
    job_dispatcher: JobDispatcher | None = None,
    local_stack_service: LocalStackService | None = None,
    local_odoo_reader: LocalOdooMetadataReader | None = None,
    preparation_jobs_enabled: bool = True,
    odoo_capture_jobs_enabled: bool = True,
    duckdb_lock_wait_timeout_seconds: float = 0.0,
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

    database = DuckDbDatabase(
        project_root,
        lock_wait_timeout_seconds=duckdb_lock_wait_timeout_seconds,
    )
    resolved_artifacts = artifact_store or LocalArtifactStore(project_root)
    project_repository = ProjectRepository(database)
    recipe_repository = RecipeRepository(database)
    recipe_authoring_repository = RecipeAuthoringRepository(database)
    recipe_application_repository = RecipeApplicationRepository(database)
    derived_entity_repository = DerivedEntityRepository(database)
    source_repository = SourceRepository(database, derived_entity_repository)
    schema_repository = SchemaRepository(database)
    mapping_repository = MappingRepository(database)
    mapping_field_catalog_repository = MappingFieldCatalogRepository(database)
    staging_repository = StagingRepository(database, resolved_artifacts)
    preparation_session_repository = PreparationSessionRepository(
        database,
        resolved_artifacts,
    )
    advanced_coverage_repository = AdvancedCoverageRepository(database)
    quality_repository = QualityRepository(database, project_repository)
    normalization_repository = NormalizationRepository(
        database,
        project_repository,
    )
    preflight_repository = PreflightRepository(database, project_repository)
    execution_repository = ExecutionRepository(database)
    reconciliation_repository = ReconciliationRepository(database)
    transformation_impact_repository = TransformationImpactRepository(database)
    resolved_authorization = authorization or CapabilityAuthorizationPolicy()
    resolved_secret_store = secret_store or CredentialVault()
    protected_recipe_store = ProtectedRecipeStore(
        project_root,
        resolved_secret_store,
    )
    recipes = RecipeService(
        recipe_repository,
        protected_recipe_store,
        resolved_authorization,
    )
    odoo_provenance_repository = OdooProvenanceRepository(
        database,
        resolved_artifacts,
    )
    odoo_provenance_service = OdooProvenanceService(
        project_repository,
        source_repository,
        odoo_provenance_repository,
        resolved_secret_store,
        resolved_authorization,
    )
    odoo_capture_publication = OdooCapturePublicationService(
        OdooSourceCaptureService(
            project_repository,
            source_repository,
            schema_repository,
            resolved_authorization,
        ),
        source_repository,
        odoo_provenance_service,
        odoo_provenance_repository,
        resolved_artifacts,
    )
    projects = ProjectService(project_repository, resolved_authorization)
    recipe_authoring = RecipeAuthoringService(
        recipes,
        projects,
        source_repository,
        mapping_repository,
        schema_repository,
        quality_repository,
        derived_entity_repository,
        advanced_coverage_repository,
        resolved_authorization,
        recipe_authoring_repository,
    )
    categorical_coverage = CategoricalCoverageService(
        source_repository,
        resolved_artifacts,
    )
    schema_workspace = SchemaWorkspaceService(
        project_repository,
        source_repository,
        schema_repository,
        resolved_authorization,
    )
    mapping_workspace = MappingWorkspaceService(
        source_repository,
        schema_repository,
        mapping_repository,
        resolved_authorization,
        categorical_coverage=categorical_coverage,
        transformation_impacts=transformation_impact_repository,
    )
    recipe_applications = RecipeApplicationService(
        recipes=recipes,
        projects=projects,
        project_reader=project_repository,
        sources=source_repository,
        schemas=schema_repository,
        schema_workspace=schema_workspace,
        references=advanced_coverage_repository,
        preparation=derived_entity_repository,
        applications=recipe_application_repository,
        mappings=mapping_workspace,
        categorical=categorical_coverage,
        store=protected_recipe_store,
        authorization=resolved_authorization,
    )
    quality = QualityService(
        mapping_repository,
        source_repository,
        quality_repository,
        recipe_quality=recipe_application_repository,
    )
    normalization = NormalizationService(
        normalization_repository,
        resolved_authorization,
    )
    resolution = ResolutionService(advanced_coverage_repository, staging_repository)
    preparation = PreparationService(
        project_repository,
        source_repository,
        derived_entity_repository,
        mapping_repository,
        staging_repository,
        preparation_session_repository,
        resolved_artifacts,
        resolved_authorization,
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
        project_repository,
        source_repository,
        preflight_repository,
        resolved_artifacts,
        resolved_authorization,
        advanced_coverage_repository,
        schema_repository,
        odoo_provenance_service,
    )

    def current_read_credential_binding(project: MigrationProject) -> str:
        if project.odoo_connection_mode is OdooConnectionMode.LOCAL:
            return local_read_credential_binding_hash(project)
        if project.odoo_connection_mode is not OdooConnectionMode.REMOTE:
            return ""
        try:
            credential = get_target_credential(
                resolved_secret_store,
                project,
                TargetCredentialRole.READ,
            )
        except SecretStoreError:
            return ""
        return credential.binding_hash if credential is not None else ""

    execution = ExecutionService(
        project_repository,
        preflight,
        execution_repository,
        resolved_authorization,
        require_remote_read_identity=True,
        require_remote_write_identity=True,
        current_read_credential_binding=current_read_credential_binding,
    )
    reconciliation = ReconciliationService(
        preflight,
        execution_repository,
        reconciliation_repository,
        resolved_authorization,
    )
    recipe_qualifications = RecipeQualificationService(
        recipes=recipes,
        recipe_applications=recipe_applications,
        applications=recipe_application_repository,
        mappings=mapping_repository,
        staging=staging_repository,
        quality=quality_repository,
        preflight=preflight,
        execution=execution_repository,
        reconciliation=reconciliation,
        authorization=resolved_authorization,
    )
    preparation_jobs = (
        PreparationJobManager(project_root) if preparation_jobs_enabled else None
    )
    odoo_capture_jobs = (
        OdooCaptureJobManager(odoo_capture_publication)
        if odoo_capture_jobs_enabled
        else None
    )
    resolved_connection_tester = connection_tester or _test_connection
    resolved_read_identity_probe = read_identity_probe or _probe_read_identity
    context = WebContext(
        queries=BrowserQueryService(
            project_repository,
            source_repository,
            derived_entity_repository,
            schema_repository,
            mapping_repository,
            mapping_field_catalog_repository,
            quality_repository,
            transformation_impact_repository,
        ),
        projects=projects,
        recipes=recipes,
        recipe_authoring=recipe_authoring,
        recipe_applications=recipe_applications,
        recipe_qualifications=recipe_qualifications,
        intake=SourceIntakeService(projects, resolved_artifacts),
        inspections=SourceInspectionService(
            project_repository,
            source_repository,
            resolved_artifacts,
            resolved_authorization,
        ),
        sources=SourceWorkspaceService(
            project_repository,
            source_repository,
            resolved_authorization,
            resolved_artifacts,
            schemas=schema_repository,
        ),
        derived_entities=DerivedEntityWorkspaceService(
            source_repository,
            derived_entity_repository,
            resolved_authorization,
        ),
        schema_workspace=schema_workspace,
        mapping_workspace=mapping_workspace,
        categorical_coverage=categorical_coverage,
        preparation=preparation,
        preparation_jobs=preparation_jobs,
        quality=quality,
        resolution=resolution,
        normalization=normalization,
        preflight=preflight,
        execution=execution,
        reconciliation=reconciliation,
        transformation_impacts=TransformationImpactService(
            project_repository,
            mapping_repository,
            source_repository,
            derived_entity_repository,
            transformation_impact_repository,
            resolved_artifacts,
            resolved_authorization,
        ),
        odoo_capture_publication=odoo_capture_publication,
        odoo_capture_jobs=odoo_capture_jobs,
        odoo_provenance=odoo_provenance_service,
        artifacts=resolved_artifacts,
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

    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=package_dir / "templates")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            recipes.recover_incomplete(actor=actor)
            for summary in project_repository.list():
                try:
                    project = project_repository.get(summary.project_id)
                except ProjectCompatibilityError:
                    continue
                if project.source_mode is SourceMode.ODOO:
                    odoo_provenance_repository.recover_incomplete_publications(
                        project.project_id
                    )
            yield
        finally:
            if context.preparation_jobs is not None:
                context.preparation_jobs.shutdown()
            if context.odoo_capture_jobs is not None:
                context.odoo_capture_jobs.shutdown()

    app = FastAPI(
        title="Impodo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.server = None
    app.state.templates = templates
    app.mount(
        "/static",
        StaticFiles(directory=package_dir / "static"),
        name="static",
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
        LoopbackSecurityMiddleware,
        expected_host=expected_host,
    )

    @app.exception_handler(ProjectNotFoundError)
    async def project_not_found(_request: Request, _error: ProjectNotFoundError):
        return HTMLResponse("Project not found", status_code=404)

    @app.exception_handler(RecipeNotFoundError)
    async def recipe_not_found(_request: Request, _error: RecipeNotFoundError):
        return HTMLResponse("Recipe not found", status_code=404)

    @app.exception_handler(ProjectCompatibilityError)
    async def project_incompatible(
        request: Request,
        error: ProjectCompatibilityError,
    ):
        return _render(
            request,
            "recipe_list.html",
            recipes=context.recipes.list(actor=context.actor),
            error=str(error),
            status_code=409,
        )

    for router in (
        build_lifecycle_router(context),
        build_projects_router(context),
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
