"""Expose setup for the mapping engine contained by a MigrationWorkspace."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.data_version.inspection import SourceInspectionError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceRegistrationError,
    WorkspaceStateError,
    WorkspaceStatus,
    workspace_registration_problems,
)

from ..context import WebContext
from ..forms import _revision, _secure_form
from ..presenters.common import _flash, _render, _workspace_error
from ..presenters.mapping_forms import _draft_or_redirect
from ..security import require_session
from ..source_file_commands import accept_source_uploads


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
            f"/workspaces/{workspace.workspace_id}/{destination}",
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
                f"/workspaces/{workspace.workspace_id}/{setup_page}",
                status_code=303,
            )
        return _render(
            request,
            "workspace_overview.html",
            workspace_state=workspace,
        )

    @router.get("/workspaces/{workspace_id}/files", response_class=HTMLResponse)
    async def workspace_files_form(request: Request, workspace_id: str):
        require_session(request)
        workspace = _draft_or_redirect(context, workspace_id)
        if isinstance(workspace, RedirectResponse):
            return workspace
        if workspace.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                f"/workspaces/{workspace.workspace_id}/target",
                status_code=303,
            )
        return _render(request, "workspace_files.html", workspace_state=workspace)

    @router.post("/workspaces/{workspace_id}/files")
    async def workspace_files(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "source_file"})
        workspace = _draft_or_redirect(context, workspace_id)
        if isinstance(workspace, RedirectResponse):
            return workspace
        if workspace.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                f"/workspaces/{workspace.workspace_id}/target",
                status_code=303,
            )
        try:
            await accept_source_uploads(context, workspace_id, form)
        except WorkspaceStateError as error:
            return _workspace_error(
                request,
                context,
                workspace_id,
                "workspace_files.html",
                error,
            )
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
            return _workspace_error(
                request,
                context,
                workspace_id,
                template,
                error,
                problems=error.problems,
            )
        except WorkspaceStateError as error:
            workspace = context.queries.get(workspace_id)
            return _workspace_error(
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
        if workspace.source_mode is SourceMode.FILE:
            try:
                catalogs = await run_in_threadpool(
                    context.inspections.inspect_project,
                    workspace.workspace_id,
                    actor=context.actor,
                )
            except SourceInspectionError as error:
                request.session["source_inspection_error"] = {
                    "workspace_id": workspace.workspace_id,
                    "message": str(error),
                }
            else:
                file_label = "file" if len(catalogs) == 1 else "files"
                _flash(request, f"Checked {len(catalogs)} source {file_label}.")
            return RedirectResponse(
                f"/workspaces/{workspace.workspace_id}/sources#source-files",
                status_code=303,
            )
        return RedirectResponse(
            f"/workspaces/{workspace.workspace_id}/schema",
            status_code=303,
        )

    return router
