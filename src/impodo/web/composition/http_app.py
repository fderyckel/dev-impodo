"""Install the shared Impodo HTTP surface around an assembled service graph.

Deployment compositions supply identity, scoped Workspace access, one lifecycle
router, and transport/session security. This module owns the common route set,
error handling, background-job shutdown, templates, and static assets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import ClientDisconnect

from impodo.application.shared.build_contract import ApplicationBuildContract
from impodo.application.workspace.access import (
    ScopedWorkspaceAccessQuery,
    WorkspaceAccessContext,
)
from impodo.domain.project.foundation import (
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
)
from impodo.domain.recipe.models import RecipeError
from impodo.domain.shared.access import AuthorizationError
from impodo.domain.workspace.errors import WorkspaceDatabaseBusyError
from impodo.domain.workspace.models import MigrationWorkspaceState
from impodo.domain.workspace.workbench import (
    WorkspaceStateCompatibilityError,
    WorkspaceStateNotFoundError,
)
from impodo.web.composition.page_reads import run_page_read
from impodo.web.context import WebContext
from impodo.web.diagnostics import (
    LocalDiagnosticRecorder,
    RequestDiagnosticsMiddleware,
    install_asyncio_exception_diagnostics,
    monitor_event_loop,
)
from impodo.web.presenters.common import _render
from impodo.web.request_identity import (
    RequestIdentityResolver,
    request_identity,
)
from impodo.web.routers.concepts import build_concepts_router
from impodo.web.routers.corrections import build_corrections_router
from impodo.web.routers.cutover_plans import build_cutover_plans_router
from impodo.web.routers.derived_entities import build_derived_entities_router
from impodo.web.routers.destination_matching import (
    build_destination_matching_router,
)
from impodo.web.routers.execution import build_execution_router
from impodo.web.routers.integrated_runs import build_integrated_runs_router
from impodo.web.routers.mapping import build_mapping_router
from impodo.web.routers.migration_projects import (
    build_migration_projects_router,
)
from impodo.web.routers.normalization import build_normalization_router
from impodo.web.routers.preflight import build_preflight_router
from impodo.web.routers.preparation import build_preparation_router
from impodo.web.routers.production_runs import build_production_runs_router
from impodo.web.routers.quality import build_quality_router
from impodo.web.routers.resolution import build_resolution_router
from impodo.web.routers.run_fresh_data import build_run_fresh_data_router
from impodo.web.routers.schema import build_schema_router
from impodo.web.routers.sources import build_sources_router
from impodo.web.routers.summary import build_summary_router
from impodo.web.routers.target import build_target_router
from impodo.web.routers.transfer_destination import (
    build_transfer_destination_router,
)
from impodo.web.routers.transfer_load import build_transfer_load_router
from impodo.web.routers.transfer_order import build_transfer_order_router
from impodo.web.routers.transfer_preflight import (
    build_transfer_preflight_router,
)
from impodo.web.routers.transfer_review import build_transfer_review_router
from impodo.web.routers.workspace_setup import build_workspace_setup_router
from impodo.web.security import WorkspaceAccessMiddleware
from impodo.web.workspace_journeys import (
    WorkspaceJourney,
    enforce_workspace_journey,
    workspace_route_is_allowed,
)

CLIENT_CLOSED_REQUEST_STATUS_CODE = 499
TransportSecurityInstaller = Callable[[FastAPI], None]


def install_http_application(
    context: WebContext,
    *,
    application_build_contract: ApplicationBuildContract,
    diagnostic_recorder: LocalDiagnosticRecorder | None,
    request_workspace_access: ScopedWorkspaceAccessQuery,
    identity_resolver: RequestIdentityResolver,
    lifecycle_router: APIRouter,
    install_transport_security: TransportSecurityInstaller,
) -> FastAPI:
    """Install common routes without selecting local or server security."""

    package_dir = Path(__file__).resolve().parent.parent
    templates = Jinja2Templates(directory=package_dir / "templates")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop = asyncio.get_running_loop()
        previous_exception_handler = install_asyncio_exception_diagnostics(
            loop,
            diagnostic_recorder,
        )
        event_loop_monitor = None
        if diagnostic_recorder is not None:
            diagnostic_recorder.record_lifecycle(
                "application_started",
                build_version=(
                    f"contract-{application_build_contract.contract_version}"
                ),
            )
            event_loop_monitor = asyncio.create_task(
                monitor_event_loop(diagnostic_recorder),
                name="impodo-event-loop-monitor",
            )
        try:
            yield
        finally:
            if event_loop_monitor is not None:
                event_loop_monitor.cancel()
                await asyncio.gather(event_loop_monitor, return_exceptions=True)
            if diagnostic_recorder is not None:
                diagnostic_recorder.record_lifecycle("application_stopping")
            try:
                if context.preparation_jobs is not None:
                    context.preparation_jobs.shutdown()
                if context.preflight_jobs is not None:
                    context.preflight_jobs.shutdown()
                if context.odoo_capture_jobs is not None:
                    context.odoo_capture_jobs.shutdown()
                if context.recipe_run_jobs is not None:
                    context.recipe_run_jobs.shutdown()
                if context.load_jobs is not None:
                    context.load_jobs.shutdown()
                context.correction_jobs.shutdown()
            except BaseException as error:
                if diagnostic_recorder is not None:
                    diagnostic_recorder.record_lifecycle(
                        "application_shutdown_failed",
                        exception_class=type(error).__name__,
                    )
                raise
            else:
                if diagnostic_recorder is not None:
                    diagnostic_recorder.record_lifecycle("application_stopped")
            finally:
                loop.set_exception_handler(previous_exception_handler)

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
    app.state.diagnostic_recorder = diagnostic_recorder
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
            if parts[2] == "preflight" and context.preflight_jobs is not None:
                return context.preflight_jobs.get(
                    workspace_id,
                    parts[3],
                ).access_context
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

        parts = request.url.path.strip("/").split("/")
        area = parts[2] if len(parts) >= 3 else ""
        trusted_job_path = (
            len(parts) >= 4 and parts[2] == "preparation"
        ) or (
            len(parts) >= 5 and parts[2:4] == ["load", "progress"]
        ) or (
            len(parts) >= 5 and parts[2:4] == ["sources", "odoo-capture"]
        )
        if trusted_job_path:
            # The access middleware already resolved these routes from their
            # verified job packet. Do not reopen a registry that the worker may
            # intentionally hold while publishing its result.
            return None
        workspace = context.migration_workspaces.repository.get_migration_workspace(
            access_context.workspace_id
        )
        if (
            workspace.state is MigrationWorkspaceState.CLOSED
            and area not in {"correction", "load"}
        ):
            identity = request_identity(request, context.identity_resolver)
            binding = context.corrections.get(
                access_context.workspace_id,
                actor=identity.actor,
            )
            request.session["flash"] = (
                "This completed workspace is historical evidence. Continue in "
                "its separate correction workspace."
            )
            return RedirectResponse(
                (
                    f"/workspaces/{access_context.workspace_id}/correction"
                    if binding is not None
                    else f"/projects/{access_context.project_id}"
                ),
                status_code=303,
            )

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
        access=request_workspace_access,
        identity_resolver=identity_resolver,
        trusted_context_resolver=trusted_job_context,
        route_policy=workspace_route_policy,
    )
    install_transport_security(app)
    if diagnostic_recorder is not None:
        app.add_middleware(
            RequestDiagnosticsMiddleware,
            recorder=diagnostic_recorder,
        )

    @app.exception_handler(ClientDisconnect)
    async def client_disconnected(_request: Request, _error: ClientDisconnect):
        """End an abandoned request without reporting an application failure."""

        return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS_CODE)

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

    @app.exception_handler(WorkspaceDatabaseBusyError)
    async def project_data_temporarily_busy(
        _request: Request,
        _error: WorkspaceDatabaseBusyError,
    ):
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Impodo is finishing a local task</title></head>
  <body>
    <main>
      <h1>Impodo is finishing another local task</h1>
      <p>Wait a moment, then reload this page. No Odoo records were changed.</p>
      <p><a href="">Try again</a></p>
    </main>
  </body>
</html>""",
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )

    @app.exception_handler(RecipeError)
    async def recipe_error(_request: Request, error: RecipeError):
        return HTMLResponse(str(error), status_code=422)

    @app.exception_handler(WorkspaceStateCompatibilityError)
    async def project_incompatible(
        request: Request,
        error: WorkspaceStateCompatibilityError,
    ):
        identity = request_identity(request, context.identity_resolver)
        projects = await run_page_read(
            context.project_queries.list_visible_project_summaries,
            scope=identity.organization_scope,
            authorization=identity.authorization,
        )
        return _render(
            request,
            "project_list.html",
            projects=projects,
            unavailable_projects=context.unavailable_projects,
            error=str(error),
            status_code=409,
        )

    for router in (
        lifecycle_router,
        build_concepts_router(),
        build_migration_projects_router(context),
        build_integrated_runs_router(context),
        build_run_fresh_data_router(context),
        build_cutover_plans_router(context),
        build_production_runs_router(context),
        build_workspace_setup_router(context),
        build_target_router(context),
        build_transfer_destination_router(context),
        build_destination_matching_router(context),
        build_transfer_order_router(context),
        build_transfer_review_router(context),
        build_transfer_preflight_router(context),
        build_transfer_load_router(context),
        build_sources_router(context),
        build_schema_router(context),
        build_derived_entities_router(context),
        build_mapping_router(context),
        build_quality_router(context.quality_routes()),
        build_preparation_router(context),
        build_resolution_router(context),
        build_normalization_router(context),
        build_summary_router(context),
        build_preflight_router(context),
        build_execution_router(
            context,
            diagnostic_recorder=diagnostic_recorder,
        ),
        build_corrections_router(context),
    ):
        app.include_router(router)

    return app
