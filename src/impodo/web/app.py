"""FastAPI composition root for the local Impodo application."""

from __future__ import annotations

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
from ..application.transformation_impact_service import TransformationImpactService
from ..artifacts import ArtifactStore, LocalArtifactStore
from ..derived_entities import DerivedEntityWorkspaceService
from ..intake import SourceIntakeService
from ..inspection import SourceInspectionService
from ..jobs import InlineJobDispatcher, JobDispatcher
from ..local_odoo_reader import LocalOdooMetadataReader
from ..local_stack import LocalStackService
from ..project_store import DuckDbProjectRepository
from ..projects import ProjectNotFoundError, ProjectService
from ..readiness import BrowserReadinessService
from ..secrets import CredentialVault, SecretStore
from ..workspace import (
    MappingWorkspaceService,
    SchemaWorkspaceService,
    SourceWorkspaceService,
)
from .context import (
    BrowserReadinessReader,
    ConnectionTester,
    ModelCatalogReader,
    SchemaReader,
    WebContext,
)
from .legacy_support import (
    _read_model_catalog,
    _read_schema,
    _test_connection,
)
from .routers.derived_entities import build_derived_entities_router
from .routers.lifecycle import build_lifecycle_router
from .routers.mapping import build_mapping_router
from .routers.normalization import build_normalization_router
from .routers.preflight import build_preflight_router
from .routers.preparation import build_preparation_router
from .routers.projects import build_projects_router
from .routers.quality import build_quality_router
from .routers.schema import build_schema_router
from .routers.sources import build_sources_router
from .routers.summary import build_summary_router
from .routers.target import build_target_router
from .security import LoopbackSecurityMiddleware


def create_local_app(
    project_root: str | Path,
    *,
    expected_host: str = "testserver",
    launch_token: str | None = None,
    session_secret: str | None = None,
    secret_store: SecretStore | None = None,
    connection_tester: ConnectionTester | None = None,
    schema_reader: SchemaReader | None = None,
    model_catalog_reader: ModelCatalogReader | None = None,
    readiness_reader: BrowserReadinessReader | None = None,
    actor: Actor = LOCAL_ACTOR,
    authorization: AuthorizationPolicy | None = None,
    artifact_store: ArtifactStore | None = None,
    job_dispatcher: JobDispatcher | None = None,
    local_stack_service: LocalStackService | None = None,
    local_odoo_reader: LocalOdooMetadataReader | None = None,
) -> FastAPI:
    """Construct the local application with injectable security/test boundaries."""

    repository = DuckDbProjectRepository(project_root)
    resolved_authorization = authorization or CapabilityAuthorizationPolicy()
    resolved_artifacts = artifact_store or LocalArtifactStore(project_root)
    projects = ProjectService(repository, resolved_authorization)
    readiness = BrowserReadinessService(
        repository,
        resolved_artifacts,
        resolved_authorization,
    )
    context = WebContext(
        queries=BrowserQueryService(repository),
        projects=projects,
        intake=SourceIntakeService(projects, resolved_artifacts),
        inspections=SourceInspectionService(
            repository,
            resolved_artifacts,
            resolved_authorization,
        ),
        sources=SourceWorkspaceService(repository, resolved_authorization),
        derived_entities=DerivedEntityWorkspaceService(
            repository,
            resolved_authorization,
        ),
        schema_workspace=SchemaWorkspaceService(
            repository,
            resolved_authorization,
        ),
        mapping_workspace=MappingWorkspaceService(
            repository,
            resolved_authorization,
        ),
        preparation=readiness.preparation,
        readiness=readiness,
        quality=readiness.quality,
        normalization=readiness.normalization,
        preflight=readiness.preflight,
        transformation_impacts=TransformationImpactService(
            repository,
            resolved_artifacts,
        ),
        artifacts=resolved_artifacts,
        actor=actor,
        authorization=resolved_authorization,
        jobs=job_dispatcher or InlineJobDispatcher(),
        secret_store=secret_store or CredentialVault(),
        launch_token=launch_token or secrets.token_urlsafe(32),
        connection_tester=connection_tester or _test_connection,
        schema_reader=schema_reader or _read_schema,
        model_catalog_reader=model_catalog_reader or _read_model_catalog,
        readiness_reader=readiness_reader,
        local_stack=local_stack_service or LocalStackService(),
        local_odoo_reader=local_odoo_reader or LocalOdooMetadataReader(),
    )

    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=package_dir / "templates")
    app = FastAPI(
        title="Impodo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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
        build_normalization_router(context),
        build_summary_router(context),
        build_preflight_router(context),
    ):
        app.include_router(router)

    return app


# Backward-compatible public name for the accepted local-only deployment.
create_app = create_local_app
