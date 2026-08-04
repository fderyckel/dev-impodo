"""FastAPI application for local Impodo project registration and inspection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import secrets
from typing import Callable
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.sessions import SessionMiddleware

from ..access import (
    Actor,
    AuthorizationError,
    AuthorizationPolicy,
    Capability,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from ..artifacts import ArtifactStore, LocalArtifactStore
from ..connectors import (
    ConnectorError,
    Json2Config,
    Json2ReadConnector,
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from ..derived_entities import (
    DerivedEntityRule,
    DerivedEntityWorkspaceService,
    RelatedDatasetRule,
    related_dataset_links,
)
from ..intake import SourceIntakeError, SourceIntakeService
from ..inspection import (
    SourceInspectionError,
    SourceInspectionOptions,
    SourceInspectionService,
)
from ..jobs import InlineJobDispatcher, JobDispatcher
from ..local_odoo_reader import (
    LocalOdooMetadataReader,
)
from ..local_stack import (
    LocalStackError,
    LocalStackProfile,
    LocalStackService,
    ReadinessLevel,
)
from ..mapping_semantics import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarTransformPolicy,
    ScalarValueError,
    ScalarValueSource,
    canonicalize_scalar_value,
    mapping_issue_fingerprint,
)
from ..project_store import DuckDbProjectRepository
from ..projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectError,
    ProjectNotFoundError,
    ProjectRegistrationError,
    ProjectService,
    ProjectStatus,
    registration_problems,
)
from ..readiness import (
    BrowserReadinessService,
    MANIFEST_NAME,
    ReadinessError,
)
from ..reporting import (
    ReportGenerationError,
    WORKBOOK_NAME,
    write_review_workbook,
)
from ..secrets import CredentialVault, SecretStore, SecretStoreError
from ..workspace import (
    MappingWorkspaceService,
    OdooModelCatalog,
    OdooModelSummary,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SchemaWorkspaceService,
    SourceWorkspaceService,
    WorkspaceError,
)
from .security import LoopbackSecurityMiddleware, require_csrf, require_session


SOURCE_SYSTEMS = (
    "Dynamics AX 2012",
    "Dynamics 365",
    "Salesforce",
    "Excel or manual files",
    "Another ERP or CRM",
    "Other",
)
ODOO_APPLICATIONS = (
    "Accounting",
    "Contacts",
    "Inventory",
    "Manufacturing",
    "Purchase",
    "Sales",
    "Custom applications",
)
_MANUAL_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MANUAL_FIELD_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_APPLICATION_MODULE_PREFIXES = {
    "Accounting": ("account", "analytic"),
    "Contacts": ("contacts",),
    "Inventory": ("stock", "product", "uom"),
    "Manufacturing": ("mrp", "maintenance", "quality"),
    "Purchase": ("purchase",),
    "Sales": ("sale", "crm"),
}


ConnectionTester = Callable[[MigrationProject, str], str]
SchemaReader = Callable[[MigrationProject, str], MetadataSnapshot]
ModelCatalogReader = Callable[[MigrationProject, str], RecordSnapshot]
BrowserReadinessReader = Callable[
    [
        MigrationProject,
        tuple[MetadataRequest, ...],
        tuple[RecordRequest, ...],
    ],
    tuple[MetadataSnapshot, RecordSnapshot],
]


@dataclass(slots=True)
class WebContext:
    repository: DuckDbProjectRepository
    projects: ProjectService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    sources: SourceWorkspaceService
    derived_entities: DerivedEntityWorkspaceService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    readiness: BrowserReadinessService
    artifacts: ArtifactStore
    actor: Actor
    authorization: AuthorizationPolicy
    jobs: JobDispatcher
    secret_store: SecretStore
    launch_token: str
    connection_tester: ConnectionTester
    schema_reader: SchemaReader
    model_catalog_reader: ModelCatalogReader
    readiness_reader: BrowserReadinessReader | None
    local_stack: LocalStackService
    local_odoo_reader: LocalOdooMetadataReader


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
    context = WebContext(
        repository=repository,
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
        readiness=BrowserReadinessService(
            repository,
            resolved_artifacts,
            resolved_authorization,
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

    @app.get("/launch")
    async def launch(request: Request, token: str = ""):
        if not secrets.compare_digest(token, context.launch_token):
            raise HTTPException(status_code=401, detail="Invalid launch token")
        context.launch_token = ""
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/projects", status_code=303)

    @app.get("/")
    async def root(request: Request):
        require_session(request)
        return RedirectResponse("/projects", status_code=303)

    @app.get("/projects", response_class=HTMLResponse)
    async def project_list(request: Request):
        require_session(request)
        return _render(
            request,
            "project_list.html",
            projects=context.repository.list(),
        )

    @app.get("/projects/new", response_class=HTMLResponse)
    async def new_project_form(request: Request):
        require_session(request)
        return _render(
            request,
            "project_new.html",
            source_systems=SOURCE_SYSTEMS,
            values={},
        )

    @app.post("/projects/new")
    async def new_project(request: Request):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "name", "source_system"})
        values = _form_values(form)
        try:
            project = context.projects.create_project(
                actor=context.actor,
                name=values.get("name", ""),
                source_system=values.get("source_system", ""),
            )
        except ProjectError as error:
            return _render(
                request,
                "project_new.html",
                source_systems=SOURCE_SYSTEMS,
                values=values,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/details",
            status_code=303,
        )

    @app.get("/projects/{project_id}")
    async def open_project(request: Request, project_id: str):
        require_session(request)
        project = context.repository.get(project_id)
        destination = (
            "summary" if project.status is ProjectStatus.REGISTERED else "details"
        )
        return RedirectResponse(
            f"/projects/{project.project_id}/{destination}",
            status_code=303,
        )

    @app.get("/projects/{project_id}/details", response_class=HTMLResponse)
    async def project_details_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render(
            request,
            "project_details.html",
            project=project,
            source_systems=SOURCE_SYSTEMS,
        )

    @app.post("/projects/{project_id}/details")
    async def project_details(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "name",
                "source_system",
                "export_status",
                "export_date",
                "description",
            },
        )
        try:
            project = context.projects.update_details(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                name=_text(form, "name"),
                source_system=_text(form, "source_system"),
                export_status=_text(form, "export_status"),
                export_date=_text(form, "export_date"),
                description=_text(form, "description"),
            )
        except ProjectError as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_details.html",
                error,
                source_systems=SOURCE_SYSTEMS,
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/governance",
            status_code=303,
        )

    @app.get("/projects/{project_id}/governance", response_class=HTMLResponse)
    async def project_governance_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render(request, "project_governance.html", project=project)

    @app.post("/projects/{project_id}/governance")
    async def project_governance(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "data_manager",
                "functional_owner",
                "business_unit",
                "data_classification",
                "retention_days",
                "support_access",
            },
        )
        try:
            project = context.projects.update_governance(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                data_manager=_text(form, "data_manager"),
                functional_owner=_text(form, "functional_owner"),
                business_unit=_text(form, "business_unit"),
                data_classification=_text(form, "data_classification"),
                retention_days=int(_text(form, "retention_days")),
                support_access="support_access" in form,
            )
        except (ProjectError, ValueError) as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_governance.html",
                error,
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/files",
            status_code=303,
        )

    @app.get("/projects/{project_id}/files", response_class=HTMLResponse)
    async def project_files_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render(request, "project_files.html", project=project)

    @app.post("/projects/{project_id}/files")
    async def project_files(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "source_file"},
        )
        upload = form.get("source_file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _project_error(
                request,
                context,
                project_id,
                "project_files.html",
                SourceIntakeError("Choose a CSV or XLSX file"),
            )
        try:
            await run_in_threadpool(
                context.intake.accept,
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                display_name=upload.filename,
                stream=upload.file,
            )
        except ProjectError as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_files.html",
                error,
            )
        finally:
            await upload.close()
        return RedirectResponse(
            f"/projects/{project_id}/files",
            status_code=303,
        )

    @app.get("/projects/{project_id}/target", response_class=HTMLResponse)
    async def project_target_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render_target(
            request,
            context,
            project,
            open_local_stack=request.query_params.get("local_stack") == "1",
        )

    @app.post("/projects/{project_id}/local-stack/select-config")
    async def select_local_stack_config(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        try:
            _require_local_stack_access(context, project)
            selected = context.local_stack.pick_config()
            if selected is None:
                _flash(request, "No Odoo configuration was selected.")
            else:
                await run_in_threadpool(
                    context.local_stack.select_config,
                    project_id,
                    selected,
                )
        except LocalStackError as error:
            return _render_target(
                request,
                context,
                project,
                error=str(error),
                status_code=422,
                open_local_stack=True,
            )
        return RedirectResponse(
            f"/projects/{project_id}/target?local_stack=1",
            status_code=303,
        )

    @app.post("/projects/{project_id}/local-stack/refresh")
    async def refresh_local_stack(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        _require_local_stack_access(context, project)
        await run_in_threadpool(context.local_stack.refresh, project_id)
        return RedirectResponse(
            f"/projects/{project_id}/target?local_stack=1",
            status_code=303,
        )

    @app.post("/projects/{project_id}/local-stack/start")
    async def start_local_stack(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "confirm_start"})
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        try:
            _require_local_stack_start(context, project)
            if _text(form, "confirm_start") != "1":
                raise LocalStackError(
                    "Confirm the detected paths before starting the local stack."
                )
            await run_in_threadpool(context.local_stack.start, project_id)
        except LocalStackError as error:
            return _render_target(
                request,
                context,
                project,
                error=str(error),
                status_code=422,
                open_local_stack=True,
            )
        _flash(request, "Local stack startup check completed.")
        return RedirectResponse(
            f"/projects/{project_id}/target?local_stack=1",
            status_code=303,
        )

    @app.post("/projects/{project_id}/local-stack/control")
    async def control_local_stack(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "confirm_control", "action"},
        )
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        action = _text(form, "action")
        try:
            if _text(form, "confirm_control") != "1":
                raise LocalStackError(
                    "Confirm control of the Impodo-managed services first."
                )
            if action == "stop":
                _require_local_stack_stop(context, project)
                await run_in_threadpool(context.local_stack.stop, project_id)
                message = "Impodo-managed local services stopped."
            elif action == "restart":
                _require_local_stack_stop(context, project)
                _require_local_stack_start(context, project)
                await run_in_threadpool(context.local_stack.restart, project_id)
                message = "Impodo-managed local services restarted."
            else:
                raise LocalStackError("Choose Stop or Restart.")
        except LocalStackError as error:
            return _render_target(
                request,
                context,
                project,
                error=str(error),
                status_code=422,
                open_local_stack=True,
            )
        _flash(request, message)
        return RedirectResponse(
            f"/projects/{project_id}/target?local_stack=1",
            status_code=303,
        )

    @app.post("/projects/{project_id}/target")
    async def project_target(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "odoo_connection_mode",
                "odoo_base_url",
                "odoo_database",
                "intended_applications",
                "api_key",
                "remember_api_key",
                "action",
            },
        )
        local_test_requested = False
        show_local_results = False
        try:
            project = context.projects.update_target(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                odoo_connection_mode=_text(form, "odoo_connection_mode"),
                odoo_base_url=_text(form, "odoo_base_url"),
                odoo_database=_text(form, "odoo_database"),
                intended_applications=form.getlist("intended_applications"),
            )
            action = _text(form, "action")
            local_test_requested = (
                action == "test"
                and project.odoo_connection_mode is OdooConnectionMode.LOCAL
            )
            submitted_key = _text(form, "api_key")
            credential_id = _target_credential_id(project)
            if submitted_key:
                context.secret_store.set(
                    credential_id,
                    submitted_key,
                    persistent="remember_api_key" in form,
                )
            if action == "test":
                local_profile = _selected_local_profile(context, project)
                if local_profile is not None:
                    show_local_results = True
                    status = await run_in_threadpool(
                        context.local_stack.refresh,
                        project_id,
                    )
                    local_profile = _selected_local_profile(context, project)
                    if local_profile is None:
                        raise LocalStackError(
                            "Choose and validate odoo.conf before testing "
                            "database access."
                        )
                    blocked_checks = tuple(
                        check.label
                        for check in status.checks
                        if check.key != "api"
                        and check.level is not ReadinessLevel.READY
                    )
                    if blocked_checks:
                        raise LocalStackError(
                            "Local connection checks failed: "
                            f"{', '.join(blocked_checks)}."
                        )
                    fingerprint = await run_in_threadpool(
                        context.local_odoo_reader.get_target_fingerprint,
                        project,
                        local_profile,
                    )
                    context.local_stack.mark_connection_ready(
                        project_id,
                        database=fingerprint.database,
                        odoo_version=fingerprint.odoo_version,
                    )
                    result = (
                        "Read-only local connection succeeded: "
                        f"{fingerprint.database} / Odoo {fingerprint.odoo_version}"
                    )
                else:
                    api_key = context.secret_store.get(credential_id)
                    if not api_key:
                        if (
                            project.odoo_connection_mode
                            is OdooConnectionMode.LOCAL
                        ):
                            raise SecretStoreError(
                                "Local mode does not require an API key. "
                                "Choose and validate odoo.conf with the local "
                                "readiness assistant first."
                            )
                        raise SecretStoreError(
                            "Enter an Odoo API key for this exact remote target "
                            "to test"
                        )
                    result = await run_in_threadpool(
                        context.connection_tester,
                        project,
                        api_key,
                    )
                _flash(request, result)
                target_url = f"/projects/{project_id}/target"
                if show_local_results:
                    target_url = f"{target_url}?local_stack=1"
                return RedirectResponse(
                    target_url,
                    status_code=303,
                )
        except (
            ProjectError,
            SecretStoreError,
            ConnectorError,
            LocalStackError,
            WorkspaceError,
        ) as error:
            if local_test_requested:
                context.local_stack.mark_connection_error(
                    project_id,
                    detail=str(error),
                )
            return _render_target(
                request,
                context,
                context.repository.get(project_id),
                error=str(error),
                status_code=422,
                open_local_stack=local_test_requested,
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/review",
            status_code=303,
        )

    @app.get("/projects/{project_id}/review", response_class=HTMLResponse)
    async def project_review(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render(
            request,
            "project_review.html",
            project=project,
            problems=registration_problems(project),
        )

    @app.post("/projects/{project_id}/register")
    async def register_project(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            project = context.projects.register(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
            )
        except ProjectRegistrationError as error:
            project = context.repository.get(project_id)
            return _render(
                request,
                "project_review.html",
                project=project,
                problems=error.problems,
                error="The project is not ready to register",
                status_code=422,
            )
        except ProjectError as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_review.html",
                error,
                problems=registration_problems(
                    context.repository.get(project_id)
                ),
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/summary",
            status_code=303,
        )

    @app.get("/projects/{project_id}/summary", response_class=HTMLResponse)
    async def project_summary(request: Request, project_id: str):
        require_session(request)
        return _render_summary(request, context, project_id)

    @app.post("/projects/{project_id}/summary/check")
    async def check_project_data(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.repository.get(project_id)

        def reader(metadata_requests, record_requests):
            return _read_readiness_snapshots(
                context,
                project,
                metadata_requests,
                record_requests,
            )

        try:
            await run_in_threadpool(
                context.readiness.run,
                project_id,
                reader=reader,
                actor=context.actor,
            )
        except (
            ConnectorError,
            ProjectError,
            ReadinessError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_summary(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Data readiness check completed.")
        return RedirectResponse(
            f"/projects/{project_id}/summary",
            status_code=303,
        )

    @app.get("/projects/{project_id}/summary/manifest")
    async def download_readiness_manifest(request: Request, project_id: str):
        require_session(request)
        report = context.readiness.current_report(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        path = _readiness_report_path(
            context,
            project_id,
            report.run_id,
            MANIFEST_NAME,
        )
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Readiness manifest not found")
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"impodo-{project_id[:8]}-preflight.json",
        )

    @app.post("/projects/{project_id}/summary/package")
    async def generate_readiness_package(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        report = context.readiness.current_report(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        if report.status != "READY":
            return _render_summary(
                request,
                context,
                project_id,
                error="Resolve the rows that need attention before creating the package.",
                status_code=422,
            )
        manifest_path = _readiness_report_path(
            context,
            project_id,
            report.run_id,
            MANIFEST_NAME,
        )
        workbook_path = _readiness_report_path(
            context,
            project_id,
            report.run_id,
            WORKBOOK_NAME,
        )
        try:
            await run_in_threadpool(
                write_review_workbook,
                manifest_path,
                workbook_path,
            )
        except (OSError, ReportGenerationError) as error:
            return _render_summary(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Review package created.")
        return RedirectResponse(
            f"/projects/{project_id}/summary",
            status_code=303,
        )

    @app.get("/projects/{project_id}/summary/workbook")
    async def download_readiness_workbook(request: Request, project_id: str):
        require_session(request)
        report = context.readiness.current_report(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        path = _readiness_report_path(
            context,
            project_id,
            report.run_id,
            WORKBOOK_NAME,
        )
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Review package not found")
        return FileResponse(
            path,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            filename=f"impodo-{project_id[:8]}-review.xlsx",
        )

    @app.get("/projects/{project_id}/sources", response_class=HTMLResponse)
    async def project_sources(request: Request, project_id: str):
        require_session(request)
        project = context.repository.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            return RedirectResponse(
                f"/projects/{project.project_id}/details",
                status_code=303,
            )
        return _render(
            request,
            "project_sources.html",
            project=project,
            catalogs=context.repository.get_source_catalogs(project_id),
            configurations={
                item.file_id: item
                for item in context.repository.get_source_configurations(project_id)
            },
        )

    @app.post("/projects/{project_id}/sources/inspect")
    async def inspect_project_sources(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.repository.get(project_id)
        try:
            catalogs = await run_in_threadpool(
                context.inspections.inspect_project,
                project_id,
                actor=context.actor,
            )
        except SourceInspectionError as error:
            return _render(
                request,
                "project_sources.html",
                project=project,
                catalogs=context.repository.get_source_catalogs(project_id),
                configurations={
                    item.file_id: item
                    for item in context.repository.get_source_configurations(project_id)
                },
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Inspected {len(catalogs)} source file(s) against their "
            "registered hashes.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/sources",
            status_code=303,
        )

    @app.post("/projects/{project_id}/sources/{file_id}/configure")
    async def configure_project_source(
        request: Request,
        project_id: str,
        file_id: str,
    ):
        form = await request.form()
        catalogs = context.repository.get_source_catalogs(project_id)
        catalog = next(
            (item for item in catalogs if item.file_id == file_id),
            None,
        )
        if catalog is None:
            raise HTTPException(status_code=404, detail="Source catalog not found")
        allowed = {
            "csrf_token",
            "action",
            "encoding",
            "delimiter",
            "warnings_acknowledged",
        }
        for index, _table in enumerate(catalog.tables):
            allowed.update({f"selected_{index}", f"header_row_{index}"})
        _secure_form(request, form, allowed)
        worksheet_rows: list[tuple[str, int]] = []
        selected: list[str] = []
        try:
            for index, table in enumerate(catalog.tables):
                header_text = _text(form, f"header_row_{index}").strip()
                if table.kind == "WORKSHEET" and header_text:
                    worksheet_rows.append((table.table_key, int(header_text)))
                if _text(form, f"selected_{index}"):
                    selected.append(table.table_key)
            header_row = int(_text(form, "header_row_0") or "1")
            options = SourceInspectionOptions(
                encoding=_text(form, "encoding").strip() or None,
                delimiter=_decode_delimiter(_text(form, "delimiter")),
                csv_header_row=header_row,
                worksheet_header_rows=tuple(worksheet_rows),
            )
            refreshed = await run_in_threadpool(
                context.inspections.inspect_file,
                project_id,
                file_id,
                options=options,
                actor=context.actor,
            )
            if _text(form, "action") == "confirm":
                refreshed_keys = {table.table_key for table in refreshed.tables}
                selected = [key for key in selected if key in refreshed_keys]
                context.sources.confirm_source(
                    project_id,
                    file_id,
                    selected_table_keys=selected,
                    warnings_acknowledged=bool(
                        _text(form, "warnings_acknowledged")
                    ),
                    actor=context.actor,
                )
                _flash(request, f"Confirmed {refreshed.display_name}.")
            else:
                _flash(request, f"Updated preview for {refreshed.display_name}.")
        except (SourceInspectionError, WorkspaceError, ValueError) as error:
            return _render(
                request,
                "project_sources.html",
                project=context.repository.get(project_id),
                catalogs=context.repository.get_source_catalogs(project_id),
                configurations={
                    item.file_id: item
                    for item in context.repository.get_source_configurations(project_id)
                },
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{project_id}/sources",
            status_code=303,
        )

    @app.get("/projects/{project_id}/datasets", response_class=HTMLResponse)
    async def project_datasets(request: Request, project_id: str):
        require_session(request)
        project = context.repository.get(project_id)
        choices = _dataset_choices(context, project_id)
        return _render(
            request,
            "project_datasets.html",
            project=project,
            choices=choices,
            selection=context.repository.get_source_selection(project_id),
        )

    @app.post("/projects/{project_id}/datasets/freeze")
    async def freeze_project_datasets(request: Request, project_id: str):
        form = await request.form()
        choices = _dataset_choices(context, project_id)
        allowed = {"csrf_token"} | {
            f"dataset_name_{index}" for index, _choice in enumerate(choices)
        }
        _secure_form(request, form, allowed)
        names = {
            (choice["file_id"], choice["table_key"]): _text(
                form, f"dataset_name_{index}"
            )
            for index, choice in enumerate(choices)
        }
        try:
            selection = context.sources.freeze_selection(
                project_id,
                dataset_names=names,
                actor=context.actor,
            )
        except WorkspaceError as error:
            return _render(
                request,
                "project_datasets.html",
                project=context.repository.get(project_id),
                choices=choices,
                selection=context.repository.get_source_selection(project_id),
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Frozen source selection version {selection.version}.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    @app.get("/projects/{project_id}/schema", response_class=HTMLResponse)
    async def project_schema(request: Request, project_id: str):
        require_session(request)
        return _render_schema(request, context, project_id)

    @app.post("/projects/{project_id}/schema/local-config")
    async def select_schema_local_config(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.repository.get(project_id)
        try:
            _require_local_stack_access(context, project)
            selected = await run_in_threadpool(context.local_stack.pick_config)
            if selected is None:
                _flash(request, "No Odoo configuration was selected.")
            else:
                await run_in_threadpool(
                    context.local_stack.select_config,
                    project_id,
                    selected,
                )
                profile = _selected_local_profile(context, project)
                if profile is None:
                    raise LocalStackError(
                        "The selected odoo.conf could not be validated."
                    )
                _flash(
                    request,
                    "Selected the local odoo.conf for keyless read-only "
                    "model discovery.",
                )
        except (LocalStackError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/derived-entities",
        response_class=HTMLResponse,
    )
    async def project_derived_entities(request: Request, project_id: str):
        require_session(request)
        return _render_derived_entities(request, context, project_id)

    @app.post("/projects/{project_id}/derived-entities/save")
    async def save_project_derived_entity(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "expected_parent_version",
                "source_binding",
                "output_dataset_name",
                "target_model",
                "target_name_field",
                "external_id_namespace",
                "parent_separator",
                "blank_policy",
            },
        )
        source_binding = _text(form, "source_binding")
        if "|" not in source_binding:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error="Choose a frozen source column",
                status_code=422,
            )
        source_dataset_id, source_column_key = source_binding.split("|", 1)
        try:
            plan, rule = context.derived_entities.save_rule(
                project_id,
                output_dataset_name=_text(form, "output_dataset_name"),
                source_dataset_id=source_dataset_id,
                source_column_key=source_column_key,
                target_model=_text(form, "target_model"),
                target_name_field=_text(form, "target_name_field"),
                external_id_namespace=_text(form, "external_id_namespace"),
                parent_separator=_text(form, "parent_separator") or None,
                blank_policy=_text(form, "blank_policy"),
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=context.actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Saved derived dataset {rule.output_dataset_name} in plan "
            f"version {plan.version}.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    @app.post("/projects/{project_id}/derived-entities/related/preview")
    async def preview_project_related_datasets(
        request: Request,
        project_id: str,
    ):
        form = await request.form()
        fields = {
            "csrf_token",
            "expected_parent_version",
            "source_dataset_id",
            "parent_dataset_name",
            "child_dataset_name",
            "parent_key_column_key",
            "scope_column_key",
            "child_key_column_key",
            "blank_policy",
        }
        _secure_form(request, form, fields)
        try:
            rule, preview = context.derived_entities.preview_related_split(
                project_id,
                source_dataset_id=_text(form, "source_dataset_id"),
                parent_dataset_name=_text(form, "parent_dataset_name"),
                child_dataset_name=_text(form, "child_dataset_name"),
                parent_key_column_key=_text(form, "parent_key_column_key"),
                scope_column_key=_text(form, "scope_column_key") or None,
                child_key_column_key=_text(form, "child_key_column_key"),
                blank_policy=_text(form, "blank_policy"),
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        return _render_derived_entities(
            request,
            context,
            project_id,
            pending_related={"rule": rule, "preview": preview},
        )

    @app.post("/projects/{project_id}/derived-entities/related/save")
    async def save_project_related_datasets(
        request: Request,
        project_id: str,
    ):
        form = await request.form()
        fields = {
            "csrf_token",
            "expected_parent_version",
            "source_dataset_id",
            "parent_dataset_name",
            "child_dataset_name",
            "parent_key_column_key",
            "scope_column_key",
            "child_key_column_key",
            "blank_policy",
        }
        _secure_form(request, form, fields)
        try:
            plan, rule = context.derived_entities.save_related_split(
                project_id,
                source_dataset_id=_text(form, "source_dataset_id"),
                parent_dataset_name=_text(form, "parent_dataset_name"),
                child_dataset_name=_text(form, "child_dataset_name"),
                parent_key_column_key=_text(form, "parent_key_column_key"),
                scope_column_key=_text(form, "scope_column_key") or None,
                child_key_column_key=_text(form, "child_key_column_key"),
                blank_policy=_text(form, "blank_policy"),
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=context.actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Created related datasets {rule.parent_dataset_name} and "
                f"{rule.child_dataset_name} in plan version {plan.version}."
            ),
        )
        return RedirectResponse(
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    @app.post(
        "/projects/{project_id}/derived-entities/{rule_id}/delete"
    )
    async def delete_project_derived_entity(
        request: Request,
        project_id: str,
        rule_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_parent_version"},
        )
        try:
            plan = context.derived_entities.delete_rule(
                project_id,
                rule_id,
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=context.actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Removed the derived-entity rule; plan version {plan.version} is current.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    @app.post("/projects/{project_id}/schema/models/refresh")
    async def refresh_project_models(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.repository.get(project_id)
        try:
            local_profile = _selected_local_profile(context, project)
            if local_profile is not None:
                snapshot = await run_in_threadpool(
                    context.local_odoo_reader.get_model_catalog,
                    project,
                    local_profile,
                )
            else:
                api_key = context.secret_store.get(
                    _target_credential_id(project)
                )
                if not api_key:
                    raise WorkspaceError(
                        _missing_schema_reader_message(project)
                    )
                snapshot = await run_in_threadpool(
                    context.model_catalog_reader,
                    project,
                    api_key,
                )
            catalog = context.schema_workspace.discover_models(
                project_id,
                snapshot,
                actor=context.actor,
            )
            if local_profile is not None:
                context.local_stack.mark_metadata_ready(
                    project_id,
                    database=catalog.database,
                    odoo_version=catalog.odoo_version,
                    model_count=len(catalog.models),
                )
        except (
            ConnectorError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_schema(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Loaded {len(catalog.models)} persistent models from Odoo.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/schema/scope",
        name="redirect_project_schema_scope",
        include_in_schema=False,
    )
    async def redirect_project_schema_scope(request: Request, project_id: str):
        require_session(request)
        return RedirectResponse(
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.post(
        "/projects/{project_id}/schema",
        name="update_project_schema_scope",
    )
    @app.post(
        "/projects/{project_id}/schema/scope",
        name="update_project_schema_scope_legacy",
        include_in_schema=False,
    )
    async def update_project_schema_scope(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "permitted_models"})
        try:
            permitted_models = _submitted_model_scope(form)
            model_catalog = context.repository.get_odoo_model_catalog(project_id)
            if model_catalog:
                available = {model.name for model in model_catalog.models}
                unknown = [
                    model for model in permitted_models if model not in available
                ]
                if unknown:
                    raise ProjectError(
                        f"{unknown[0]} is not in the refreshed Odoo model catalogue"
                    )
            context.projects.update_schema_scope(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                permitted_models=permitted_models,
            )
        except ProjectError as error:
            return _render_schema(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Saved the permitted model scope. Capture the schema again before mapping.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.post("/projects/{project_id}/schema/capture")
    async def capture_project_schema(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.repository.get(project_id)
        try:
            local_profile = _selected_local_profile(context, project)
            if local_profile is not None:
                snapshot = await run_in_threadpool(
                    context.local_odoo_reader.get_model_metadata,
                    project,
                    local_profile,
                    project.intended_models,
                )
            else:
                api_key = context.secret_store.get(
                    _target_credential_id(project)
                )
                if not api_key:
                    raise WorkspaceError(
                        _missing_schema_reader_message(project)
                    )
                snapshot = await run_in_threadpool(
                    context.schema_reader,
                    project,
                    api_key,
                )
            schema = context.schema_workspace.capture(
                project_id,
                snapshot,
                actor=context.actor,
            )
            if local_profile is not None:
                catalog = context.repository.get_odoo_model_catalog(project_id)
                context.local_stack.mark_metadata_ready(
                    project_id,
                    database=schema.database,
                    odoo_version=schema.odoo_version,
                    model_count=(
                        len(catalog.models)
                        if catalog is not None
                        else len(schema.models)
                    ),
                )
        except (
            ConnectorError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_schema(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, f"Captured {len(schema.models)} permitted Odoo model(s).")
        return RedirectResponse(
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.post("/projects/{project_id}/schema/local-draft")
    async def create_local_schema_draft(request: Request, project_id: str):
        form = await request.form()
        project = context.repository.get(project_id)
        allowed = {"csrf_token", "acknowledge_local_draft"} | {
            name
            for index, _model in enumerate(project.intended_models)
            for name in (
                f"manual_model_label_{index}",
                f"manual_fields_{index}",
            )
        }
        _secure_form(request, form, allowed)
        try:
            if not _checked(form, "acknowledge_local_draft"):
                raise WorkspaceError(
                    "Acknowledge that this local schema is unverified"
                )
            schema = context.schema_workspace.capture_local_manual(
                project_id,
                _manual_schema_models(project, form),
                actor=context.actor,
            )
        except (ProjectError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Created an unverified local schema draft for "
                f"{len(schema.models)} permitted Odoo model(s)."
            ),
        )
        return RedirectResponse(
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.post("/projects/{project_id}/schema/govern")
    async def govern_project_schema(request: Request, project_id: str):
        form = await request.form()
        schema = context.repository.get_odoo_schema_catalog(project_id)
        if schema is None:
            raise HTTPException(status_code=422, detail="Odoo schema missing")
        allowed = {"csrf_token"} | {
            name
            for index, _model in enumerate(schema.models)
            for name in (
                f"key_fields_{index}",
                f"scope_fields_{index}",
                f"key_description_{index}",
            )
        }
        _secure_form(request, form, allowed)
        definitions: list[BusinessKeyDefinition] = []
        for index, model in enumerate(schema.models):
            key_fields = _comma_values(
                _text(form, f"key_fields_{index}")
            )
            if not key_fields:
                continue
            scope_fields = _comma_values(
                _text(form, f"scope_fields_{index}")
            )
            definitions.append(
                BusinessKeyDefinition(
                    key_id=_business_key_id(
                        model.name, key_fields, scope_fields
                    ),
                    model=model.name,
                    key_fields=key_fields,
                    scope_fields=scope_fields,
                    description=_text(form, f"key_description_{index}"),
                    status=BusinessKeyStatus.CONFIRMED,
                )
            )
        try:
            governance = context.schema_workspace.govern(
                project_id,
                business_keys=definitions,
                actor=context.actor,
            )
        except (ValueError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Confirmed schema governance version {governance.version} "
                f"with {len(governance.business_keys)} business key(s)."
            ),
        )
        return RedirectResponse(
            f"/projects/{project_id}/mapping",
            status_code=303,
        )

    @app.get("/projects/{project_id}/mapping", response_class=HTMLResponse)
    async def project_mapping(request: Request, project_id: str):
        require_session(request)
        return _render_mapping(request, context, project_id)

    @app.post("/projects/{project_id}/mapping/save")
    async def save_project_mapping(request: Request, project_id: str):
        form = await request.form()
        selection = context.repository.get_mapping_source_selection(project_id)
        if selection is None:
            raise HTTPException(status_code=422, detail="Source selection missing")

        schema = context.repository.get_odoo_schema_catalog(project_id)
        governance = context.repository.get_schema_governance(project_id)
        if schema is None:
            raise HTTPException(status_code=422, detail="Odoo schema missing")
        allowed = _mapping_allowed_fields(form, selection, schema)
        _secure_form(request, form, allowed)
        try:
            action = _text(form, "action")
            if action not in {"draft", "submit"}:
                raise WorkspaceError("Choose save draft or submit")
            expected_parent = _optional_int(
                _text(form, "expected_parent_version")
            )
            datasets = _mapping_datasets_from_form(
                form,
                selection,
                schema,
                governance,
            )
            revision, validation, submission = (
                context.mapping_workspace.save_definition(
                    project_id,
                    datasets=datasets,
                    expected_parent_version=expected_parent,
                    submit=action == "submit",
                    warning_acknowledgements=_texts(
                        form, "warning_acknowledgement"
                    ),
                    actor=context.actor,
                )
            )
        except (ValueError, WorkspaceError) as error:
            return _render_mapping(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        if submission is not None:
            _flash(
                request,
                f"Mapping submitted as version {revision.version}.",
            )
        else:
            _flash(
                request,
                (
                    f"Saved mapping version {revision.version}: "
                    f"{validation.status.value.replace('_', ' ').casefold()}."
                ),
            )
        return RedirectResponse(
            f"/projects/{project_id}/mapping",
            status_code=303,
        )

    @app.post("/quit", response_class=HTMLResponse)
    async def quit_impodo(request: Request):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        server = request.app.state.server
        if server is not None:
            server.should_exit = True
        return _render(request, "goodbye.html")

    return app


# Backward-compatible public name for the accepted local-only deployment.
create_app = create_local_app


def _test_connection(project: MigrationProject, api_key: str) -> str:
    if project.odoo_connection_mode is None:
        raise ProjectError("Choose Local Odoo or Remote Odoo")
    connector = Json2ReadConnector(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
        )
    )
    metadata = connector.get_model_metadata(
        (MetadataRequest(model="res.partner", fields=("id",)),)
    )
    fingerprint = metadata.fingerprint
    if (
        fingerprint.odoo_version != "unknown"
        and not fingerprint.odoo_version.startswith("19.")
    ):
        raise ProjectError(
            f"Expected Odoo 19, received Odoo {fingerprint.odoo_version}"
        )
    return (
        f"Read-only {project.odoo_connection_mode.value.casefold()} connection "
        f"succeeded: {fingerprint.database} / Odoo {fingerprint.odoo_version}"
    )


def _selected_local_profile(
    context: WebContext,
    project: MigrationProject,
) -> LocalStackProfile | None:
    """Return the session-bound profile only when it matches this target."""

    if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
        return None
    status = context.local_stack.get(project.project_id)
    profile = status.profile
    if profile is None:
        return None
    if profile.base_url.rstrip("/") != project.odoo_base_url.rstrip("/"):
        raise WorkspaceError(
            "The selected odoo.conf points to "
            f"{profile.base_url}, but this project targets "
            f"{project.odoo_base_url}. Choose the matching configuration or "
            "correct the project target."
        )
    return profile


def _missing_schema_reader_message(project: MigrationProject) -> str:
    if project.odoo_connection_mode is OdooConnectionMode.LOCAL:
        return (
            "Local mode does not require an API key. Choose and validate "
            "odoo.conf on this page before loading models or fields."
        )
    return "No API key is stored for this exact remote Odoo target."


def _read_schema(project: MigrationProject, api_key: str) -> MetadataSnapshot:
    """Read all fields once per explicitly permitted Odoo model."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before schema capture")
    if not project.intended_models:
        raise ProjectError("Add at least one permitted technical Odoo model")
    connector = Json2ReadConnector(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
        )
    )
    return connector.get_model_metadata(
        tuple(
            MetadataRequest(model=model, fields=(), all_fields=True)
            for model in project.intended_models
        )
    )


def _read_model_catalog(
    project: MigrationProject,
    api_key: str,
) -> RecordSnapshot:
    """Read lightweight persistent-model choices from the exact Odoo target."""

    if project.odoo_connection_mode is None:
        raise ProjectError("Configure the Odoo target before model discovery")
    connector = Json2ReadConnector(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
        )
    )
    return connector.get_records(
        (
            RecordRequest(
                model="ir.model",
                fields=(
                    "name",
                    "model",
                    "abstract",
                    "transient",
                    "modules",
                    "state",
                ),
                domain=(
                    ("abstract", "=", False),
                    ("transient", "=", False),
                ),
            ),
        )
    )


def _read_readiness_snapshots(
    context: WebContext,
    project: MigrationProject,
    metadata_requests: tuple[MetadataRequest, ...],
    record_requests: tuple[RecordRequest, ...],
) -> tuple[MetadataSnapshot, RecordSnapshot]:
    """Read one consistent target snapshot through the configured boundary."""

    if context.readiness_reader is not None:
        return context.readiness_reader(
            project,
            metadata_requests,
            record_requests,
        )
    local_profile = _selected_local_profile(context, project)
    if project.odoo_connection_mode is OdooConnectionMode.LOCAL:
        if local_profile is None:
            raise WorkspaceError(
                "Choose and validate the matching local odoo.conf before "
                "checking data."
            )
        return context.local_odoo_reader.get_preflight_snapshots(
            project,
            local_profile,
            metadata_requests,
            record_requests,
        )
    api_key = context.secret_store.get(_target_credential_id(project))
    if not api_key:
        raise SecretStoreError(
            "Enter an Odoo API key for this remote target before checking data."
        )
    if project.odoo_connection_mode is None:
        raise WorkspaceError("Configure the Odoo target before checking data")
    connector = Json2ReadConnector(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
        )
    )
    metadata = connector.get_model_metadata(metadata_requests)
    records = connector.get_records(record_requests)
    return metadata, records


def _target_credential_id(project: MigrationProject) -> str:
    """Bind a stored API key to one project and exact Odoo destination."""

    connection_mode = (
        project.odoo_connection_mode.value
        if project.odoo_connection_mode
        else ""
    )
    target = "\0".join(
        (
            project.project_id,
            connection_mode,
            project.odoo_base_url,
            project.odoo_database,
        )
    ).encode("utf-8")
    digest = hashlib.sha256(target).hexdigest()[:24]
    return f"{project.project_id}:{digest}"


def _render(
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    **context,
):
    values = {
        "csrf_token": request.session.get("csrf_token", ""),
        "flash": request.session.pop("flash", None),
        **context,
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template_name,
        context=values,
        status_code=status_code,
    )


def _render_target(
    request: Request,
    context: WebContext,
    project: MigrationProject,
    *,
    error: str | None = None,
    status_code: int = 200,
    open_local_stack: bool = False,
):
    return _render(
        request,
        "project_target.html",
        project=project,
        applications=ODOO_APPLICATIONS,
        local_stack=context.local_stack.get(project.project_id),
        open_local_stack=open_local_stack,
        error=error,
        status_code=status_code,
    )


def _render_summary(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    project = context.repository.get(project_id)
    revision = context.repository.get_mapping_revision(project_id)
    submission = (
        context.repository.get_mapping_submission(project_id, revision.version)
        if revision is not None
        else None
    )
    report = context.readiness.current_report(project_id)
    status_filter = request.query_params.get("status", "").strip()
    if status_filter not in {"", "ready", "needs_review", "blocked"}:
        status_filter = ""
    dataset_filter = request.query_params.get("dataset", "").strip()
    available_datasets = {
        item.dataset for item in (report.datasets if report else ())
    }
    if dataset_filter not in available_datasets:
        dataset_filter = ""
    rows = tuple(
        item
        for item in (report.rows if report else ())
        if (not status_filter or item.status == status_filter)
        and (not dataset_filter or item.dataset == dataset_filter)
    )
    return _render(
        request,
        "project_summary.html",
        project=project,
        revision=revision,
        submission=submission,
        readiness=report,
        readiness_rows=rows,
        review_workbook_ready=(
            report is not None
            and _readiness_report_path(
                context,
                project_id,
                report.run_id,
                WORKBOOK_NAME,
            ).is_file()
        ),
        status_filter=status_filter,
        dataset_filter=dataset_filter,
        error=error,
        status_code=status_code,
    )


def _readiness_report_path(
    context: WebContext,
    project_id: str,
    run_id: str,
    filename: str,
) -> Path:
    try:
        canonical_run_id = str(UUID(run_id))
    except (ValueError, AttributeError) as error:
        raise HTTPException(
            status_code=404,
            detail="Readiness report not found",
        ) from error
    if filename not in {MANIFEST_NAME, WORKBOOK_NAME}:
        raise HTTPException(status_code=404, detail="Report file not found")
    reports_root = (
        context.repository.project_directory(project_id) / "reports"
    ).resolve()
    target = (reports_root / canonical_run_id / filename).resolve()
    if target.parent.parent != reports_root:
        raise HTTPException(status_code=404, detail="Report file not found")
    return target


def _require_local_stack_access(
    context: WebContext,
    project: MigrationProject,
) -> None:
    try:
        context.authorization.require(
            context.actor,
            Capability.LOCAL_STACK_INSPECT,
            project_id=project.project_id,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to inspect the local Odoo stack",
        ) from error
    if (
        project.odoo_connection_mode is not None
        and project.odoo_connection_mode is not OdooConnectionMode.LOCAL
    ):
        raise LocalStackError(
            "The local readiness assistant is available only in Local Odoo mode."
        )


def _require_local_stack_start(
    context: WebContext,
    project: MigrationProject,
) -> None:
    _require_local_stack_access(context, project)
    try:
        context.authorization.require(
            context.actor,
            Capability.LOCAL_STACK_START,
            project_id=project.project_id,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to start the local Odoo stack",
        ) from error


def _require_local_stack_stop(
    context: WebContext,
    project: MigrationProject,
) -> None:
    _require_local_stack_access(context, project)
    try:
        context.authorization.require(
            context.actor,
            Capability.LOCAL_STACK_STOP,
            project_id=project.project_id,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to stop the local Odoo stack",
        ) from error


def _secure_form(
    request: Request,
    form: FormData,
    allowed_fields: set[str],
) -> None:
    require_csrf(request, _text(form, "csrf_token"))
    unexpected = {key for key, _value in form.multi_items()} - allowed_fields
    if unexpected:
        raise HTTPException(status_code=422, detail="Unexpected form fields")


def _revision(form: FormData) -> int:
    try:
        return int(_text(form, "revision"))
    except ValueError as error:
        raise ProjectError("Invalid project revision") from error


def _text(form: FormData, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


def _form_values(form: FormData) -> dict[str, str]:
    return {
        key: value
        for key, value in form.items()
        if isinstance(value, str) and key != "csrf_token"
    }


def _split_models(value: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in re.split(r"[,\r\n]+", value)
        if part.strip()
    )


def _submitted_model_scope(form: FormData) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            model
            for value in form.getlist("permitted_models")
            for model in _split_models(str(value))
        )
    )


def _manual_schema_models(
    project: MigrationProject,
    form: FormData,
) -> tuple[SchemaModel, ...]:
    """Parse the explicitly entered local-development schema contract."""

    return tuple(
        SchemaModel(
            name=model_name,
            label=(
                _text(form, f"manual_model_label_{index}").strip()
                or model_name
            ),
            fields=_manual_schema_fields(
                model_name,
                _text(form, f"manual_fields_{index}"),
            ),
        )
        for index, model_name in enumerate(project.intended_models)
    )


def _manual_schema_fields(
    model_name: str,
    value: str,
) -> tuple[SchemaField, ...]:
    """Parse ``name | label | type | required | readonly | relation | inverse``."""

    fields: list[SchemaField] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if not 3 <= len(parts) <= 7:
            raise WorkspaceError(
                f"{model_name} line {line_number} must contain name, label, "
                "and type separated by |"
            )
        name, label, field_type, *optional = parts
        if not _MANUAL_FIELD_NAME.fullmatch(name):
            raise WorkspaceError(
                f"{model_name} line {line_number} has an invalid field name"
            )
        if not label or len(label) > 200:
            raise WorkspaceError(
                f"{model_name} line {line_number} needs a field label"
            )
        if not _MANUAL_FIELD_TYPE.fullmatch(field_type):
            raise WorkspaceError(
                f"{model_name} line {line_number} has an invalid field type"
            )
        required, readonly, relation, relation_field = (
            optional + ["", "", "", ""]
        )[:4]
        fields.append(
            SchemaField(
                name=name,
                label=label,
                type=field_type,
                required=_manual_schema_boolean(
                    required,
                    model_name,
                    line_number,
                    "required",
                ),
                readonly=_manual_schema_boolean(
                    readonly,
                    model_name,
                    line_number,
                    "readonly",
                ),
                relation=relation or None,
                relation_field=relation_field or None,
                selection=(),
            )
        )
    return tuple(fields)


def _manual_schema_boolean(
    value: str,
    model_name: str,
    line_number: int,
    label: str,
) -> bool:
    normalized = value.casefold()
    if normalized in {"", "0", "false", "no"}:
        return False
    if normalized in {"1", "true", "yes"}:
        return True
    raise WorkspaceError(
        f"{model_name} line {line_number} has an invalid {label} value"
    )


def _decode_delimiter(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.casefold() == "tab":
        return "\t"
    return cleaned


def _dataset_choices(
    context: WebContext,
    project_id: str,
) -> tuple[dict[str, str], ...]:
    catalogs = {
        item.file_id: item
        for item in context.repository.get_source_catalogs(project_id)
    }
    choices: list[dict[str, str]] = []
    for configuration in context.repository.get_source_configurations(project_id):
        catalog = catalogs.get(configuration.file_id)
        if catalog is None or catalog.content_hash != configuration.catalog_hash:
            continue
        tables = {table.table_key: table for table in catalog.tables}
        for table_key in configuration.selected_table_keys:
            table = tables.get(table_key)
            if table is None:
                continue
            default_name = re.sub(
                r"[^a-z0-9]+",
                "_",
                f"{Path(catalog.display_name).stem}_{table.name}".casefold(),
            ).strip("_")[:63]
            if not default_name or not default_name[0].isalpha():
                default_name = f"dataset_{len(choices) + 1}"
            choices.append(
                {
                    "file_id": catalog.file_id,
                    "file_name": catalog.display_name,
                    "table_key": table.table_key,
                    "table_name": table.name,
                    "default_name": default_name,
                    "row_count": str(table.row_count),
                    "column_count": str(table.column_count),
                }
            )
    return tuple(choices)


def _render_derived_entities(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    pending_related: dict[str, object] | None = None,
):
    project = context.repository.get(project_id)
    selection = context.repository.get_source_selection(project_id)
    plan = context.repository.get_derived_entity_plan(project_id)
    schema = context.repository.get_odoo_schema_catalog(project_id)
    model_choices = tuple(
        sorted(
            (
                {"name": item.name, "label": item.label}
                for item in (schema.models if schema else ())
            ),
            key=lambda item: str(item["name"]),
        )
    )
    if not model_choices:
        model_choices = tuple(
            {"name": name, "label": name}
            for name in sorted(project.intended_models)
        )
    source_choices = tuple(
        {
            "value": f"{dataset.dataset_id}|{column.stable_key}",
            "dataset_name": dataset.name,
            "column_name": column.source_name,
            "candidate_type": column.candidate_type,
        }
        for dataset in (selection.datasets if selection else ())
        for column in dataset.columns
    )
    rule_views: list[dict[str, object]] = []
    related_rule_views: list[dict[str, object]] = []
    for rule in (plan.rules if plan else ()):
        try:
            preview = (
                context.derived_entities.preview(project_id, rule)
                if isinstance(rule, DerivedEntityRule)
                else context.derived_entities.preview_related(project_id, rule)
            )
            preview_error = None
        except WorkspaceError as preview_failure:
            preview = None
            preview_error = str(preview_failure)
        target = (
            rule_views
            if isinstance(rule, DerivedEntityRule)
            else related_rule_views
        )
        target.append(
            {
                "rule": rule,
                "preview": preview,
                "preview_error": preview_error,
            }
        )
    split_sources = {
        view["rule"].source_dataset_id for view in related_rule_views
    }
    related_source_views = tuple(
        {
            "dataset": dataset,
            "columns": dataset.columns,
            "has_split": dataset.dataset_id in split_sources,
            "parent_name_default": _related_dataset_name_default(
                dataset.name,
                "parents",
            ),
            "child_name_default": _related_dataset_name_default(
                dataset.name,
                "lines",
            ),
        }
        for dataset in (selection.datasets if selection else ())
    )
    namespace = re.sub(
        r"[^a-z0-9]+",
        "_",
        project.source_system.casefold(),
    ).strip("_")[:40]
    if not namespace or not namespace[0].isalpha():
        namespace = "legacy"
    return _render(
        request,
        "project_derived_entities.html",
        project=project,
        selection=selection,
        plan=plan,
        source_choices=source_choices,
        model_choices=model_choices,
        rule_views=tuple(rule_views),
        related_rule_views=tuple(related_rule_views),
        related_source_views=related_source_views,
        pending_related=pending_related,
        namespace_default=namespace,
        error=error,
        status_code=status_code,
    )


def _related_dataset_name_default(source_name: str, suffix: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", source_name.casefold()).strip("_")
    if not base or not base[0].isalpha():
        base = "source"
    return f"{base[:54]}_{suffix}"


def _schema_model_choices(
    project: MigrationProject,
    catalog: OdooModelCatalog | None,
) -> tuple[dict[str, object], ...]:
    selected = set(project.intended_models)
    models = list(catalog.models) if catalog else []
    known = {model.name for model in models}
    models.extend(
        OdooModelSummary(
            name=name,
            label=name,
            modules=(),
            state="unknown",
        )
        for name in sorted(selected - known)
    )
    choices = [
        {
            "name": model.name,
            "label": model.label,
            "modules": model.modules,
            "state": model.state,
            "selected": model.name in selected,
            "in_focus": _model_matches_application_scope(
                model,
                project.intended_applications,
            ),
        }
        for model in models
    ]
    return tuple(
        sorted(
            choices,
            key=lambda item: (
                not bool(item["selected"]),
                not bool(item["in_focus"]),
                str(item["label"]).casefold(),
                str(item["name"]),
            ),
        )
    )


def _model_matches_application_scope(
    model: OdooModelSummary,
    applications: tuple[str, ...],
) -> bool:
    if not applications or "Custom applications" in applications:
        return True
    if "Contacts" in applications and (
        model.name.startswith(("res.partner", "res.country", "res.lang"))
        or "contacts" in model.modules
    ):
        return True
    for application in applications:
        for prefix in _APPLICATION_MODULE_PREFIXES.get(application, ()):
            if any(
                module == prefix or module.startswith(f"{prefix}_")
                for module in model.modules
            ):
                return True
    return False


def _render_schema(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    project = context.repository.get(project_id)
    model_catalog = context.repository.get_odoo_model_catalog(project_id)
    model_choices = _schema_model_choices(project, model_catalog)
    schema = context.repository.get_odoo_schema_catalog(project_id)
    governance = context.repository.get_schema_governance(project_id)
    governed_by_model = (
        {item.model: item for item in governance.business_keys}
        if governance
        else {}
    )
    return _render(
        request,
        "project_schema.html",
        project=project,
        selection=context.repository.get_source_selection(project_id),
        model_catalog=model_catalog,
        model_choices=model_choices,
        focus_model_count=sum(
            1 for choice in model_choices if choice["in_focus"]
        ),
        schema=schema,
        schema_field_count=(
            sum(len(model.fields) for model in schema.models)
            if schema is not None
            else 0
        ),
        governance=governance,
        governed_by_model=governed_by_model,
        local_stack=context.local_stack.get(project_id),
        manual_schema_by_model=(
            {model.name: model for model in schema.models}
            if schema and schema.origin is SchemaOrigin.LOCAL_MANUAL
            else {}
        ),
        error=error,
        status_code=status_code,
    )


def _render_mapping(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    selection = context.repository.get_mapping_source_selection(project_id)
    preparation_plan = context.repository.get_derived_entity_plan(project_id)
    schema = context.repository.get_odoo_schema_catalog(project_id)
    governance = context.repository.get_schema_governance(project_id)
    revision = context.repository.get_mapping_revision(project_id)
    validation = (
        context.repository.get_mapping_validation(
            project_id, revision.version
        )
        if revision
        else None
    )
    submission = (
        context.repository.get_mapping_submission(
            project_id, revision.version
        )
        if revision
        else None
    )
    legacy_draft = context.repository.get_mapping_draft(project_id)
    source_catalogs = (
        context.repository.get_source_catalogs(project_id)
        if selection is not None
        else ()
    )
    dataset_views = (
        _mapping_dataset_views(
            selection,
            schema,
            governance,
            revision.definition.datasets if revision else (),
            source_catalogs,
            {
                index: request.query_params.get(f"target_model_{index}", "")
                for index, _item in enumerate(selection.datasets)
            },
            related_dataset_links(preparation_plan),
        )
        if selection and schema
        else ()
    )
    warning_issues = tuple(
        {
            "issue": item,
            "fingerprint": mapping_issue_fingerprint(item),
        }
        for item in (validation.issues if validation else ())
        if item.severity == "warning"
    )
    return _render(
        request,
        "project_mapping.html",
        project=context.repository.get(project_id),
        selection=selection,
        schema=schema,
        governance=governance,
        revision=revision,
        validation=validation,
        submission=submission,
        legacy_draft=legacy_draft,
        dataset_views=dataset_views,
        warning_issues=warning_issues,
        error=error,
        status_code=status_code,
    )


def _mapping_dataset_views(
    selection,
    schema,
    governance,
    existing_datasets,
    source_catalogs=(),
    selected_models=None,
    related_links=(),
) -> tuple[dict[str, object], ...]:
    existing_by_id = {item.dataset_id: item for item in existing_datasets}
    models = {item.name: item for item in schema.models}
    keys = tuple(governance.business_keys) if governance else ()
    confirmed = tuple(
        item
        for item in keys
        if item.status is BusinessKeyStatus.CONFIRMED
    )
    link_by_child = {item.child_dataset_id: item for item in related_links}
    link_by_parent = {item.parent_dataset_id: item for item in related_links}
    parent_ids = {item.parent_dataset_id for item in related_links}
    result: list[dict[str, object]] = []
    for dataset_index, source_dataset in enumerate(selection.datasets):
        source_samples = _mapping_source_samples(
            source_dataset,
            source_catalogs,
        )
        existing = existing_by_id.get(source_dataset.dataset_id)
        selected_override = (
            selected_models.get(dataset_index, "")
            if selected_models is not None
            else ""
        )
        selected_model_name = (
            selected_override
            if selected_override in models
            else (
                existing.target_model
                if existing and existing.target_model in models
                else (
                    next(
                        (
                            item.model
                            for item in confirmed
                            if item.model in models
                        ),
                        schema.models[0].name if schema.models else "",
                    )
                )
            )
        )
        model = models.get(selected_model_name)
        model_keys = tuple(
            item for item in confirmed if item.model == selected_model_name
        )
        existing_identity_fields = tuple(
            target
            for component in (
                (*existing.target_identity, *existing.target_scope)
                if existing
                else ()
            )
            for target in component.target_fields
        )
        selected_key = next(
            (
                item
                for item in model_keys
                if (*item.key_fields, *item.scope_fields)
                == existing_identity_fields
            ),
            model_keys[0] if model_keys else None,
        )
        field_by_name = (
            {item.name: item for item in model.fields} if model else {}
        )
        existing_components = {
            item.target_fields[0]: item
            for item in (
                (*existing.target_identity, *existing.target_scope)
                if existing
                else ()
            )
            if len(item.target_fields) == 1
        }
        identity_rows: list[dict[str, object]] = []
        if selected_key is not None:
            for target_field in (
                *selected_key.key_fields,
                *selected_key.scope_fields,
            ):
                metadata = field_by_name.get(target_field)
                component = existing_components.get(target_field)
                related_keys = tuple(
                    item
                    for item in confirmed
                    if metadata is not None
                    and item.model == metadata.relation
                )
                selected_related_key = _resolver_business_key(
                    component.resolver if component else None,
                    related_keys,
                )
                identity_rows.append(
                    {
                        "target_field": target_field,
                        "scope": target_field in selected_key.scope_fields,
                        "metadata": metadata,
                        "relational": (
                            metadata is not None
                            and metadata.type == "many2one"
                        ),
                        "selected_sources": (
                            component.source_column_keys if component else ()
                        ),
                        "related_keys": related_keys,
                        "selected_related_key": selected_related_key,
                    }
                )
        identity_targets = {
            row["target_field"] for row in identity_rows
        }
        scalar_by_target = (
            {item.target_field: item for item in existing.fields}
            if existing
            else {}
        )
        all_scalar_fields = tuple(
            field
            for field in (model.fields if model else ())
            if field.type not in {"many2one", "many2many", "one2many"}
        )
        scalar_rows = tuple(
            {
                "index": field_index,
                "metadata": field,
                "mapping": scalar_by_target.get(field.name),
                "canonical_type": _canonical_mapping_type(field.type),
                "source_samples": source_samples,
                "preview": _scalar_mapping_preview(
                    scalar_by_target.get(field.name),
                    source_samples,
                ),
            }
            for field_index, field in enumerate(all_scalar_fields)
            if field.name not in identity_targets
        )
        relation_by_target = (
            {item.target_field: item for item in existing.relationships}
            if existing
            else {}
        )
        relation_rows: list[dict[str, object]] = []
        all_relation_fields = tuple(
            field
            for field in (model.fields if model else ())
            if field.type in {"many2one", "many2many", "one2many"}
        )
        for relation_index, field in enumerate(all_relation_fields):
            if field.name in identity_targets:
                continue
            mapping = relation_by_target.get(field.name)
            related_keys = tuple(
                item for item in confirmed if item.model == field.relation
            )
            relation_rows.append(
                {
                    "index": relation_index,
                    "metadata": field,
                    "mapping": mapping,
                    "related_keys": related_keys,
                    "selected_key": _resolver_business_key(
                        mapping.resolver if mapping else None,
                        related_keys,
                    ),
                }
            )
        result.append(
            {
                "index": dataset_index,
                "source": source_dataset,
                "mapping": existing,
                "selected_model": selected_model_name,
                "model": model,
                "models": schema.models,
                "business_keys": model_keys,
                "selected_key": selected_key,
                "identity_rows": tuple(identity_rows),
                "scalar_rows": scalar_rows,
                "relation_rows": tuple(relation_rows),
                "other_datasets": tuple(
                    item
                    for item in selection.datasets
                    if item.dataset_id != source_dataset.dataset_id
                ),
                "related_role": (
                    "child"
                    if source_dataset.dataset_id in link_by_child
                    else (
                        "parent"
                        if source_dataset.dataset_id in parent_ids
                        else None
                    )
                ),
                "recommended_source_identity": (
                    link_by_child[source_dataset.dataset_id].child_identity_column_keys
                    if source_dataset.dataset_id in link_by_child
                    else (
                        link_by_parent[
                            source_dataset.dataset_id
                        ].reference_column_keys
                        if source_dataset.dataset_id in link_by_parent
                        else ()
                    )
                ),
            }
        )
    views_by_dataset = {
        view["source"].dataset_id: view for view in result
    }
    for view in result:
        source = view["source"]
        link = link_by_child.get(source.dataset_id)
        if link is None:
            continue
        parent_view = views_by_dataset.get(link.parent_dataset_id)
        parent_model = parent_view["selected_model"] if parent_view else None
        for relation_row in view["relation_rows"]:
            metadata = relation_row["metadata"]
            if metadata.relation != parent_model:
                continue
            relation_row["recommended_dataset_id"] = link.parent_dataset_id
            relation_row["recommended_source_columns"] = (
                link.reference_column_keys
            )
    return tuple(result)


def _mapping_source_samples(
    source_dataset,
    source_catalogs,
) -> dict[str, tuple[str | None, ...]]:
    catalog = next(
        (
            item
            for item in source_catalogs
            if item.file_id == source_dataset.file_id
            and item.source_sha256 == source_dataset.source_sha256
            and item.content_hash == source_dataset.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog is not None else ())
            if item.table_key == source_dataset.table_key
        ),
        None,
    )
    if table is None:
        return {}
    result: dict[str, tuple[str | None, ...]] = {}
    for column in source_dataset.columns:
        values = tuple(
            row[column.ordinal - 1]
            for row in table.preview_rows
            if column.ordinal > 0 and column.ordinal <= len(row)
        )
        result[column.stable_key] = values[:3]
    return result


def _scalar_mapping_preview(
    mapping: ScalarFieldMapping | None,
    source_samples: dict[str, tuple[str | None, ...]],
) -> dict[str, str] | None:
    if mapping is None:
        return None
    if mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
        return {
            "raw": "Not sent",
            "proposed": "Odoo runtime default",
            "status": "deferred",
        }
    raw: object = None
    if mapping.value_source is ScalarValueSource.CONSTANT:
        raw = mapping.literal_value
    elif mapping.source_column_key:
        samples = source_samples.get(mapping.source_column_key, ())
        raw = samples[0] if samples else None
    try:
        proposed = canonicalize_scalar_value(mapping, raw)
    except ScalarValueError as error:
        return {
            "raw": _display_mapping_value(raw),
            "proposed": str(error),
            "status": "error",
        }
    return {
        "raw": _display_mapping_value(raw),
        "proposed": _display_mapping_value(proposed),
        "status": "ok",
    }


def _display_mapping_value(value: object) -> str:
    if value is None:
        return "∅"
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _mapping_allowed_fields(form, selection, schema) -> set[str]:
    allowed = {
        "csrf_token",
        "action",
        "expected_parent_version",
        "warning_acknowledgement",
    }
    model_names = {item.name for item in schema.models}
    models = {item.name: item for item in schema.models}
    for dataset_index, _dataset in enumerate(selection.datasets):
        allowed.update(
            {
                f"target_model_{dataset_index}",
                f"mode_{dataset_index}",
                f"on_existing_{dataset_index}",
                f"source_identity_{dataset_index}",
                f"business_key_{dataset_index}",
            }
        )
        target_model = _text(form, f"target_model_{dataset_index}")
        if target_model not in model_names:
            continue
        model = models[target_model]
        for identity_index in range(len(model.fields)):
            allowed.update(
                {
                    f"identity_source_{dataset_index}_{identity_index}",
                    f"identity_resolver_key_{dataset_index}_{identity_index}",
                }
            )
        scalar_fields = [
            item
            for item in model.fields
            if item.type not in {"many2one", "many2many", "one2many"}
        ]
        for field_index, _field in enumerate(scalar_fields):
            allowed.update(
                {
                    f"scalar_value_source_{dataset_index}_{field_index}",
                    f"scalar_source_{dataset_index}_{field_index}",
                    f"scalar_literal_{dataset_index}_{field_index}",
                    f"scalar_type_{dataset_index}_{field_index}",
                    f"scalar_trim_{dataset_index}_{field_index}",
                    f"scalar_collapse_{dataset_index}_{field_index}",
                    f"scalar_empty_null_{dataset_index}_{field_index}",
                    f"scalar_case_{dataset_index}_{field_index}",
                    f"scalar_decimal_locale_{dataset_index}_{field_index}",
                    f"scalar_date_format_{dataset_index}_{field_index}",
                    f"scalar_timezone_{dataset_index}_{field_index}",
                    f"scalar_compare_{dataset_index}_{field_index}",
                    f"scalar_validate_only_{dataset_index}_{field_index}",
                    f"scalar_required_{dataset_index}_{field_index}",
                    f"scalar_required_create_{dataset_index}_{field_index}",
                    f"scalar_null_{dataset_index}_{field_index}",
                }
            )
        relation_fields = [
            item
            for item in model.fields
            if item.type in {"many2one", "many2many", "one2many"}
        ]
        for relation_index, _field in enumerate(relation_fields):
            allowed.update(
                {
                    f"relation_source_{dataset_index}_{relation_index}",
                    f"relation_origin_{dataset_index}_{relation_index}",
                    f"relation_dataset_{dataset_index}_{relation_index}",
                    f"relation_key_{dataset_index}_{relation_index}",
                    f"relation_operation_{dataset_index}_{relation_index}",
                    f"relation_compare_{dataset_index}_{relation_index}",
                    f"relation_validate_only_{dataset_index}_{relation_index}",
                    f"relation_required_{dataset_index}_{relation_index}",
                    f"relation_required_create_{dataset_index}_{relation_index}",
                    f"relation_missing_{dataset_index}_{relation_index}",
                    f"relation_ambiguous_{dataset_index}_{relation_index}",
                    f"relation_null_{dataset_index}_{relation_index}",
                    f"relation_separator_{dataset_index}_{relation_index}",
                }
            )
    return allowed


def _mapping_datasets_from_form(
    form,
    selection,
    schema,
    governance,
) -> tuple[DatasetMapping, ...]:
    models = {item.name: item for item in schema.models}
    keys = {
        item.key_id: item
        for item in (
            governance.business_keys if governance is not None else ()
        )
        if item.status is BusinessKeyStatus.CONFIRMED
    }
    datasets: list[DatasetMapping] = []
    for dataset_index, source_dataset in enumerate(selection.datasets):
        target_model = _text(form, f"target_model_{dataset_index}")
        model = models.get(target_model)
        if model is None:
            raise WorkspaceError("Choose a captured target model")
        selected_key = keys.get(
            _text(form, f"business_key_{dataset_index}")
        )
        if selected_key is not None and selected_key.model != target_model:
            raise WorkspaceError("Target business key does not match its model")
        field_by_name = {item.name: item for item in model.fields}
        source_columns = {
            item.stable_key for item in source_dataset.columns
        }
        identity_components: list[IdentityComponentMapping] = []
        scope_components: list[IdentityComponentMapping] = []
        identity_targets: set[str] = set()
        key_fields = (
            (*selected_key.key_fields, *selected_key.scope_fields)
            if selected_key
            else ()
        )
        for identity_index, target_field in enumerate(key_fields):
            selected_sources = tuple(
                item
                for item in _texts(
                    form,
                    f"identity_source_{dataset_index}_{identity_index}",
                )
                if item in source_columns
            )
            metadata = field_by_name.get(target_field)
            resolver = None
            if metadata is not None and metadata.type == "many2one":
                related_key = keys.get(
                    _text(
                        form,
                        (
                            f"identity_resolver_key_{dataset_index}_"
                            f"{identity_index}"
                        ),
                    )
                )
                resolver = _target_catalog_resolver(
                    metadata.relation,
                    related_key,
                    selected_sources,
                )
            component = IdentityComponentMapping(
                source_column_keys=selected_sources,
                target_fields=(target_field,),
                value_type=(
                    "string"
                    if resolver is not None
                    else _canonical_mapping_type(
                        metadata.type if metadata else "char"
                    )
                ),
                resolver=resolver,
            )
            target = (
                scope_components
                if selected_key
                and target_field in selected_key.scope_fields
                else identity_components
            )
            target.append(component)
            identity_targets.add(target_field)

        scalar_fields = [
            item
            for item in model.fields
            if item.type not in {"many2one", "many2many", "one2many"}
        ]
        scalar_mappings: list[ScalarFieldMapping] = []
        for field_index, metadata in enumerate(scalar_fields):
            value_source_text = _text(
                form,
                f"scalar_value_source_{dataset_index}_{field_index}",
            )
            if not value_source_text or metadata.name in identity_targets:
                continue
            value_source = ScalarValueSource(value_source_text)
            source_key = _text(
                form, f"scalar_source_{dataset_index}_{field_index}"
            )
            literal_value = _text(
                form, f"scalar_literal_{dataset_index}_{field_index}"
            )
            scalar_mappings.append(
                ScalarFieldMapping(
                    target_field=metadata.name,
                    source_column_key=(
                        source_key
                        if value_source
                        in {
                            ScalarValueSource.SOURCE,
                            ScalarValueSource.SOURCE_WITH_FALLBACK,
                        }
                        else None
                    ),
                    value_source=value_source,
                    literal_value=(
                        literal_value
                        if value_source
                        in {
                            ScalarValueSource.CONSTANT,
                            ScalarValueSource.SOURCE_WITH_FALLBACK,
                        }
                        else None
                    ),
                    transform=ScalarTransformPolicy(
                        trim=_checked(
                            form,
                            f"scalar_trim_{dataset_index}_{field_index}",
                        ),
                        collapse_whitespace=_checked(
                            form,
                            f"scalar_collapse_{dataset_index}_{field_index}",
                        ),
                        empty_as_null=_checked(
                            form,
                            f"scalar_empty_null_{dataset_index}_{field_index}",
                        ),
                        case_mode=(
                            _text(
                                form,
                                f"scalar_case_{dataset_index}_{field_index}",
                            )
                            or "preserve"
                        ),
                        decimal_locale=(
                            _text(
                                form,
                                (
                                    f"scalar_decimal_locale_{dataset_index}_"
                                    f"{field_index}"
                                ),
                            )
                            or "invariant"
                        ),
                        date_format=(
                            _text(
                                form,
                                f"scalar_date_format_{dataset_index}_{field_index}",
                            )
                            or "iso"
                        ),
                        timezone=(
                            _text(
                                form,
                                f"scalar_timezone_{dataset_index}_{field_index}",
                            )
                            or "UTC"
                        ),
                    ),
                    value_type=(
                        _text(
                            form,
                            f"scalar_type_{dataset_index}_{field_index}",
                        )
                        or _canonical_mapping_type(metadata.type)
                    ),
                    compare=_checked(
                        form,
                        f"scalar_compare_{dataset_index}_{field_index}",
                    ),
                    validate_only=_checked(
                        form,
                        f"scalar_validate_only_{dataset_index}_{field_index}",
                    ),
                    required=_checked(
                        form,
                        f"scalar_required_{dataset_index}_{field_index}",
                    ),
                    required_on_create=_checked(
                        form,
                        (
                            f"scalar_required_create_{dataset_index}_"
                            f"{field_index}"
                        ),
                    ),
                    null_policy=(
                        _text(
                            form,
                            f"scalar_null_{dataset_index}_{field_index}",
                        )
                        or "distinct"
                    ),
                )
            )

        relation_fields = [
            item
            for item in model.fields
            if item.type in {"many2one", "many2many", "one2many"}
        ]
        relationships: list[RelationshipMapping] = []
        for relation_index, metadata in enumerate(relation_fields):
            if metadata.name in identity_targets:
                continue
            selected_sources = tuple(
                item
                for item in _texts(
                    form,
                    f"relation_source_{dataset_index}_{relation_index}",
                )
                if item in source_columns
            )
            if not selected_sources:
                continue
            origin = ResolverOrigin(
                _text(
                    form,
                    f"relation_origin_{dataset_index}_{relation_index}",
                )
                or ResolverOrigin.TARGET_CATALOG.value
            )
            if origin is ResolverOrigin.DATASET:
                resolver = RelationshipResolver(
                    origin=origin,
                    dataset_id=_text(
                        form,
                        f"relation_dataset_{dataset_index}_{relation_index}",
                    )
                    or None,
                )
            else:
                resolver = _target_catalog_resolver(
                    metadata.relation,
                    keys.get(
                        _text(
                            form,
                            f"relation_key_{dataset_index}_{relation_index}",
                        )
                    ),
                    selected_sources,
                )
            relationships.append(
                RelationshipMapping(
                    target_field=metadata.name,
                    kind=metadata.type,
                    source_column_keys=selected_sources,
                    resolver=resolver,
                    compare=_checked(
                        form,
                        f"relation_compare_{dataset_index}_{relation_index}",
                    ),
                    validate_only=_checked(
                        form,
                        (
                            f"relation_validate_only_{dataset_index}_"
                            f"{relation_index}"
                        ),
                    ),
                    required=_checked(
                        form,
                        f"relation_required_{dataset_index}_{relation_index}",
                    ),
                    required_on_create=_checked(
                        form,
                        (
                            f"relation_required_create_{dataset_index}_"
                            f"{relation_index}"
                        ),
                    ),
                    on_missing=(
                        _text(
                            form,
                            f"relation_missing_{dataset_index}_{relation_index}",
                        )
                        or "error"
                    ),
                    on_ambiguous=(
                        _text(
                            form,
                            (
                                f"relation_ambiguous_{dataset_index}_"
                                f"{relation_index}"
                            ),
                        )
                        or "error"
                    ),
                    operation=(
                        _text(
                            form,
                            (
                                f"relation_operation_{dataset_index}_"
                                f"{relation_index}"
                            ),
                        )
                        or "replace"
                    ),
                    separator=(
                        _text(
                            form,
                            (
                                f"relation_separator_{dataset_index}_"
                                f"{relation_index}"
                            ),
                        )
                        or ";"
                    ),
                    null_policy=(
                        _text(
                            form,
                            f"relation_null_{dataset_index}_{relation_index}",
                        )
                        or "distinct"
                    ),
                )
            )
        mode = MappingTargetMode(
            _text(form, f"mode_{dataset_index}") or "upsert"
        )
        datasets.append(
            DatasetMapping(
                dataset_id=source_dataset.dataset_id,
                target_model=target_model,
                mode=mode,
                on_existing=(
                    _text(form, f"on_existing_{dataset_index}") or "block"
                    if mode is MappingTargetMode.CREATE
                    else None
                ),
                source_identity_column_keys=tuple(
                    item
                    for item in _texts(
                        form, f"source_identity_{dataset_index}"
                    )
                    if item in source_columns
                ),
                target_identity=tuple(identity_components),
                target_scope=tuple(scope_components),
                fields=tuple(
                    sorted(
                        scalar_mappings,
                        key=lambda item: item.target_field,
                    )
                ),
                relationships=tuple(
                    sorted(
                        relationships,
                        key=lambda item: item.target_field,
                    )
                ),
            )
        )
    return tuple(datasets)


def _target_catalog_resolver(
    related_model: str | None,
    business_key,
    selected_sources: tuple[str, ...],
) -> RelationshipResolver:
    key_count = len(business_key.key_fields) if business_key else 0
    return RelationshipResolver(
        origin=ResolverOrigin.TARGET_CATALOG,
        model=related_model,
        key_mappings=tuple(
            ReferenceKeyMapping(source, target)
            for source, target in zip(
                selected_sources[:key_count],
                business_key.key_fields if business_key else (),
                strict=False,
            )
        ),
        scope_mappings=tuple(
            ReferenceKeyMapping(source, target)
            for source, target in zip(
                selected_sources[key_count:],
                business_key.scope_fields if business_key else (),
                strict=False,
            )
        ),
    )


def _resolver_business_key(resolver, candidates):
    if resolver is None or resolver.origin is not ResolverOrigin.TARGET_CATALOG:
        return candidates[0] if candidates else None
    key_fields = tuple(item.target_field for item in resolver.key_mappings)
    scope_fields = tuple(item.target_field for item in resolver.scope_mappings)
    return next(
        (
            item
            for item in candidates
            if item.key_fields == key_fields
            and item.scope_fields == scope_fields
        ),
        candidates[0] if candidates else None,
    )


def _canonical_mapping_type(odoo_type: str) -> str:
    return {
        "boolean": "boolean",
        "integer": "integer",
        "float": "decimal",
        "monetary": "decimal",
        "date": "date",
        "datetime": "datetime",
    }.get(odoo_type, "string")


def _comma_values(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in value.split(",")
        if item.strip()
    )


def _business_key_id(
    model: str,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> str:
    payload = "\0".join((model, *key_fields, "\0", *scope_fields))
    return f"key:{model}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _texts(form: FormData, name: str) -> tuple[str, ...]:
    return tuple(
        value
        for value in form.getlist(name)
        if isinstance(value, str) and value
    )


def _checked(form: FormData, name: str) -> bool:
    return _text(form, name) in {"1", "true", "on", "yes"}


def _optional_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise WorkspaceError("Invalid mapping parent version") from error


def _draft_or_redirect(
    context: WebContext,
    project_id: str,
) -> MigrationProject | RedirectResponse:
    project = context.repository.get(project_id)
    if project.status is not ProjectStatus.DRAFT:
        return RedirectResponse(
            f"/projects/{project.project_id}/summary",
            status_code=303,
        )
    return project


def _project_error(
    request: Request,
    context: WebContext,
    project_id: str,
    template_name: str,
    error: Exception,
    **extra,
):
    project = context.repository.get(project_id)
    return _render(
        request,
        template_name,
        project=project,
        error=str(error),
        status_code=422,
        **extra,
    )


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message
