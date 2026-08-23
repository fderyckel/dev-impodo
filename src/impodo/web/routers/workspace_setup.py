"""Expose setup for the mapping engine contained by a MigrationWorkspace."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ...intake import SourceIntakeError
from ...workspace_state import (
    WorkspaceStateError,
    WorkspaceRegistrationError,
    WorkspaceStatus,
    SourceMode,
    workspace_registration_problems,
)
from ..context import WebContext
from ..forms import _revision, _secure_form
from ..presenters.common import _project_error, _render
from ..presenters.mapping_forms import _draft_or_redirect
from ..security import require_session


def build_workspace_setup_router(context: WebContext) -> APIRouter:
    """Build workspace setup routes without a Recipe-root side effect."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}")
    async def open_workspace(request: Request, workspace_id: str):
        require_session(request)
        workspace = context.queries.get(workspace_id)
        destination = (
            "overview"
            if workspace.status is WorkspaceStatus.REGISTERED
            else ("files" if workspace.source_mode is SourceMode.FILE else "target")
        )
        return RedirectResponse(
            f"/workspaces/{workspace.project_id}/{destination}",
            status_code=303,
        )

    @router.get("/workspaces/{workspace_id}/overview", response_class=HTMLResponse)
    async def workspace_overview(request: Request, workspace_id: str):
        require_session(request)
        workspace = context.queries.get(workspace_id)
        if workspace.status is not WorkspaceStatus.REGISTERED:
            setup_page = (
                "files" if workspace.source_mode is SourceMode.FILE else "target"
            )
            return RedirectResponse(
                f"/workspaces/{workspace.project_id}/{setup_page}",
                status_code=303,
            )
        return _render(
            request,
            "workspace_overview.html",
            project=workspace,
        )

    @router.get("/workspaces/{workspace_id}/files", response_class=HTMLResponse)
    async def workspace_files_form(request: Request, workspace_id: str):
        require_session(request)
        workspace = _draft_or_redirect(context, workspace_id)
        if isinstance(workspace, RedirectResponse):
            return workspace
        if workspace.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                f"/workspaces/{workspace.project_id}/target",
                status_code=303,
            )
        return _render(request, "workspace_files.html", project=workspace)

    @router.post("/workspaces/{workspace_id}/files")
    async def workspace_files(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "source_file"})
        workspace = _draft_or_redirect(context, workspace_id)
        if isinstance(workspace, RedirectResponse):
            return workspace
        if workspace.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                f"/workspaces/{workspace.project_id}/target",
                status_code=303,
            )
        uploads = tuple(
            item
            for item in form.getlist("source_file")
            if isinstance(item, UploadFile) and item.filename
        )
        if not uploads:
            return _project_error(
                request,
                context,
                workspace_id,
                "workspace_files.html",
                SourceIntakeError("Choose a CSV or XLSX file"),
            )
        added = 0
        expected_revision = _revision(form)
        try:
            for upload in uploads:
                await run_in_threadpool(
                    context.intake.accept,
                    workspace_id,
                    actor=context.actor,
                    expected_revision=expected_revision,
                    display_name=upload.filename,
                    stream=upload.file,
                )
                added += 1
                expected_revision += 1
        except WorkspaceStateError as error:
            if added:
                error = SourceIntakeError(
                    f"Added {added} file{'s' if added != 1 else ''}. "
                    f"The next file could not be added: {error}"
                )
            return _project_error(
                request,
                context,
                workspace_id,
                "workspace_files.html",
                error,
            )
        finally:
            for upload in uploads:
                await upload.close()
        return RedirectResponse(
            f"/workspaces/{workspace_id}/files",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/register")
    async def register_workspace(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            workspace = context.workspace_states.register(
                workspace_id,
                actor=context.actor,
                expected_revision=_revision(form),
            )
        except WorkspaceRegistrationError as error:
            workspace = context.queries.get(workspace_id)
            template = (
                "workspace_files.html"
                if workspace.source_mode is SourceMode.FILE
                else "workspace_review.html"
            )
            return _project_error(
                request,
                context,
                workspace_id,
                template,
                error,
                problems=error.problems,
            )
        except WorkspaceStateError as error:
            workspace = context.queries.get(workspace_id)
            return _project_error(
                request,
                context,
                workspace_id,
                (
                    "workspace_files.html"
                    if workspace.source_mode is SourceMode.FILE
                    else "workspace_review.html"
                ),
                error,
                problems=workspace_registration_problems(workspace),
            )
        destination = "sources" if workspace.source_mode is SourceMode.FILE else "schema"
        return RedirectResponse(
            f"/workspaces/{workspace.project_id}/{destination}",
            status_code=303,
        )

    return router

