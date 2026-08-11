"""Translate the Stage A setup wizard into project lifecycle operations.

Layer: web route. The router creates a draft, collects project details and
governance, accepts immutable source files, configures the target, and invokes
registration. Validation and lifecycle meaning remain in ``ProjectService``;
artifact intake remains in ``SourceIntakeService``.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from ...access import AuthorizationError, Capability
from ...intake import SourceIntakeError
from ...local_stack import LocalStackError
from ...projects import (
    DataClassification,
    ProjectConflictError,
    ProjectError,
    ProjectRegistrationError,
    ProjectStatus,
    registration_problems,
)
from ...secrets import SecretStoreError
from ..security import require_session
from fastapi import APIRouter
from ..constants import SOURCE_SYSTEMS
from ..context import WebContext
from ..forms import _form_values, _revision, _secure_form, _text
from ..presenters.common import _flash, _project_error, _render
from ..presenters.mapping_forms import _draft_or_redirect
from ..target_readers import _target_credential_id


def build_projects_router(context: WebContext) -> APIRouter:
    """Build project list, setup, registration, and deletion routes."""

    router = APIRouter()

    @router.get("/projects", response_class=HTMLResponse)
    async def project_list(request: Request):
        require_session(request)
        return _render(
            request,
            "project_list.html",
            projects=context.queries.list(),
        )

    @router.post("/projects/{project_id}/delete")
    async def delete_project(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            expected_revision = _revision(form)
            project = context.queries.get(project_id)
            if project.revision != expected_revision:
                raise ProjectConflictError(
                    "The project changed in another request; reload before deleting"
                )
            if (
                context.preparation_jobs is not None
                and context.preparation_jobs.active(project.project_id) is not None
            ):
                raise ProjectConflictError(
                    "Preparation is still running. Stop it before deleting this project."
                )
            context.authorization.require(
                context.actor,
                Capability.PROJECT_DELETE,
                project_id=project.project_id,
            )
            context.local_stack.forget_project(project.project_id)
            context.remote_connections.clear(project.project_id)
            context.secret_store.delete(_target_credential_id(project))
            deleted = await run_in_threadpool(
                context.projects.delete_project,
                project.project_id,
                actor=context.actor,
                expected_revision=expected_revision,
            )
            if context.preparation_jobs is not None:
                context.preparation_jobs.delete_project_history(project.project_id)
        except AuthorizationError as error:
            raise HTTPException(
                status_code=403,
                detail="Not authorized to delete this project",
            ) from error
        except (LocalStackError, SecretStoreError, ProjectError) as error:
            return _render(
                request,
                "project_list.html",
                projects=context.queries.list(),
                error=str(error),
                status_code=422,
            )
        _flash(request, f'Deleted project "{deleted.name}".')
        return RedirectResponse("/projects", status_code=303)

    @router.get("/projects/new", response_class=HTMLResponse)
    async def new_project_form(request: Request):
        require_session(request)
        return _render(
            request,
            "project_new.html",
            source_systems=SOURCE_SYSTEMS,
            values={},
        )

    @router.post("/projects/new")
    async def new_project(request: Request):
        """Create the minimal draft and enter its governed setup sequence."""

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

    @router.get("/projects/{project_id}")
    async def open_project(request: Request, project_id: str):
        require_session(request)
        project = context.queries.get(project_id)
        destination = (
            "overview" if project.status is ProjectStatus.REGISTERED else "details"
        )
        return RedirectResponse(
            f"/projects/{project.project_id}/{destination}",
            status_code=303,
        )

    @router.get("/projects/{project_id}/overview", response_class=HTMLResponse)
    async def project_overview(request: Request, project_id: str):
        require_session(request)
        project = context.queries.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            return RedirectResponse(
                f"/projects/{project.project_id}/details",
                status_code=303,
            )
        return _render(
            request,
            "project_overview.html",
            project=project,
        )

    @router.get("/projects/{project_id}/details", response_class=HTMLResponse)
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

    @router.post("/projects/{project_id}/details")
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

    @router.get("/projects/{project_id}/governance", response_class=HTMLResponse)
    async def project_governance_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        governance_was_saved = context.queries.has_project_audit_event(
            project_id,
            "PROJECT_GOVERNANCE_UPDATED",
        )
        return _render(
            request,
            "project_governance.html",
            project=project,
            data_classification_for_form=(
                project.data_classification.value
                if governance_was_saved
                else DataClassification.INTERNAL.value
            ),
        )

    @router.post("/projects/{project_id}/governance")
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

    @router.get("/projects/{project_id}/files", response_class=HTMLResponse)
    async def project_files_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render(request, "project_files.html", project=project)

    @router.post("/projects/{project_id}/files")
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

    @router.get("/projects/{project_id}/review", response_class=HTMLResponse)
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

    @router.post("/projects/{project_id}/register")
    async def register_project(request: Request, project_id: str):
        """Register a complete draft or render every remaining problem."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            project = context.projects.register(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
            )
        except ProjectRegistrationError as error:
            project = context.queries.get(project_id)
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
                    context.queries.get(project_id)
                ),
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/overview",
            status_code=303,
        )

    return router
