"""FastAPI application for local Impodo project registration and inspection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import secrets
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.sessions import SessionMiddleware

from ..access import (
    Actor,
    AuthorizationPolicy,
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
)
from ..intake import SourceIntakeError, SourceIntakeService
from ..inspection import (
    SourceInspectionError,
    SourceInspectionOptions,
    SourceInspectionService,
)
from ..jobs import InlineJobDispatcher, JobDispatcher
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
from ..secrets import CredentialVault, SecretStore, SecretStoreError
from ..workspace import (
    FieldMapping,
    MappingWorkspaceService,
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
    "Custom UC applications",
)


ConnectionTester = Callable[[MigrationProject, str], str]
SchemaReader = Callable[[MigrationProject, str], MetadataSnapshot]


@dataclass(slots=True)
class WebContext:
    repository: DuckDbProjectRepository
    projects: ProjectService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    sources: SourceWorkspaceService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    artifacts: ArtifactStore
    actor: Actor
    authorization: AuthorizationPolicy
    jobs: JobDispatcher
    secret_store: SecretStore
    launch_token: str
    connection_tester: ConnectionTester
    schema_reader: SchemaReader


def create_local_app(
    project_root: str | Path,
    *,
    expected_host: str = "testserver",
    launch_token: str | None = None,
    session_secret: str | None = None,
    secret_store: SecretStore | None = None,
    connection_tester: ConnectionTester | None = None,
    schema_reader: SchemaReader | None = None,
    actor: Actor = LOCAL_ACTOR,
    authorization: AuthorizationPolicy | None = None,
    artifact_store: ArtifactStore | None = None,
    job_dispatcher: JobDispatcher | None = None,
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
        schema_workspace=SchemaWorkspaceService(
            repository,
            resolved_authorization,
        ),
        mapping_workspace=MappingWorkspaceService(
            repository,
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
        return _render(
            request,
            "project_target.html",
            project=project,
            applications=ODOO_APPLICATIONS,
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
                "target_environment",
                "odoo_base_url",
                "odoo_database",
                "intended_applications",
                "intended_models",
                "api_key",
                "remember_api_key",
                "action",
            },
        )
        try:
            project = context.projects.update_target(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                odoo_connection_mode=_text(form, "odoo_connection_mode"),
                target_environment=_text(form, "target_environment"),
                odoo_base_url=_text(form, "odoo_base_url"),
                odoo_database=_text(form, "odoo_database"),
                intended_applications=form.getlist("intended_applications"),
                intended_models=_split_models(_text(form, "intended_models")),
            )
            submitted_key = _text(form, "api_key")
            credential_id = _target_credential_id(project)
            if submitted_key:
                context.secret_store.set(
                    credential_id,
                    submitted_key,
                    persistent="remember_api_key" in form,
                )
            if _text(form, "action") == "test":
                api_key = context.secret_store.get(credential_id)
                if not api_key:
                    raise SecretStoreError(
                        "Enter an Odoo API key for this exact target to test"
                    )
                result = await run_in_threadpool(
                    context.connection_tester,
                    project,
                    api_key,
                )
                _flash(request, result)
                return RedirectResponse(
                    f"/projects/{project_id}/target",
                    status_code=303,
                )
        except (ProjectError, SecretStoreError, ConnectorError) as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_target.html",
                error,
                applications=ODOO_APPLICATIONS,
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
        project = context.repository.get(project_id)
        return _render(request, "project_summary.html", project=project)

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
            f"/projects/{project_id}/schema",
            status_code=303,
        )

    @app.get("/projects/{project_id}/schema", response_class=HTMLResponse)
    async def project_schema(request: Request, project_id: str):
        require_session(request)
        return _render(
            request,
            "project_schema.html",
            project=context.repository.get(project_id),
            selection=context.repository.get_source_selection(project_id),
            schema=context.repository.get_odoo_schema_catalog(project_id),
        )

    @app.post("/projects/{project_id}/schema/capture")
    async def capture_project_schema(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.repository.get(project_id)
        try:
            api_key = context.secret_store.get(_target_credential_id(project))
            if not api_key:
                raise WorkspaceError(
                    "No API key is stored for this exact Odoo target"
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
        except (
            ConnectorError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render(
                request,
                "project_schema.html",
                project=project,
                selection=context.repository.get_source_selection(project_id),
                schema=context.repository.get_odoo_schema_catalog(project_id),
                error=str(error),
                status_code=422,
            )
        _flash(request, f"Captured {len(schema.models)} permitted Odoo model(s).")
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
        selection = context.repository.get_source_selection(project_id)
        if selection is None:
            raise HTTPException(status_code=422, detail="Source selection missing")
        source_columns = [
            (dataset.name, column.source_name)
            for dataset in selection.datasets
            for column in dataset.columns
        ]
        allowed = {"csrf_token", "action"} | {
            f"target_{index}" for index, _item in enumerate(source_columns)
        }
        _secure_form(request, form, allowed)
        proposals: list[FieldMapping] = []
        for index, (dataset_name, source_column) in enumerate(source_columns):
            target = _text(form, f"target_{index}")
            if not target:
                continue
            try:
                target_model, target_field = target.split("::", 1)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid target field",
                ) from error
            proposals.append(
                FieldMapping(
                    dataset_name=dataset_name,
                    source_column=source_column,
                    target_model=target_model,
                    target_field=target_field,
                )
            )
        try:
            draft = context.mapping_workspace.save(
                project_id,
                proposals=proposals,
                submit=_text(form, "action") == "submit",
                actor=context.actor,
            )
        except WorkspaceError as error:
            return _render_mapping(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Mapping {draft.status.value.casefold()} as version {draft.version}.",
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
    if project.target_environment is None:
        raise ProjectError("Choose a DEV or TEST environment")
    connector = Json2ReadConnector(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            environment=project.target_environment.value,
            allow_insecure_loopback=(
                project.odoo_connection_mode is OdooConnectionMode.LOCAL
            ),
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
        f"succeeded: {fingerprint.environment} / "
        f"Odoo {fingerprint.odoo_version}"
    )


def _read_schema(project: MigrationProject, api_key: str) -> MetadataSnapshot:
    """Read all fields once per explicitly permitted Odoo model."""

    if project.odoo_connection_mode is None or project.target_environment is None:
        raise ProjectError("Configure the Odoo target before schema capture")
    if not project.intended_models:
        raise ProjectError("Add at least one permitted technical Odoo model")
    connector = Json2ReadConnector(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            environment=project.target_environment.value,
            allow_insecure_loopback=(
                project.odoo_connection_mode is OdooConnectionMode.LOCAL
            ),
        )
    )
    return connector.get_model_metadata(
        tuple(
            MetadataRequest(model=model, fields=(), all_fields=True)
            for model in project.intended_models
        )
    )


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


def _render_mapping(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    selection = context.repository.get_source_selection(project_id)
    schema = context.repository.get_odoo_schema_catalog(project_id)
    draft = context.repository.get_mapping_draft(project_id)
    existing = (
        {
            (entry.dataset_name, entry.source_column): (
                f"{entry.target_model}::{entry.target_field}"
            )
            for entry in draft.entries
        }
        if draft
        else {}
    )
    source_columns = (
        tuple(
            {
                "dataset": dataset.name,
                "column": column.source_name,
                "candidate_type": column.candidate_type,
                "selected": existing.get((dataset.name, column.source_name), ""),
            }
            for dataset in selection.datasets
            for column in dataset.columns
        )
        if selection
        else ()
    )
    return _render(
        request,
        "project_mapping.html",
        project=context.repository.get(project_id),
        selection=selection,
        schema=schema,
        draft=draft,
        source_columns=source_columns,
        error=error,
        status_code=status_code,
    )


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
