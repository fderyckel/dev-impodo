"""Stage-G review routes for group decisions and eligible-dataset approval."""

from __future__ import annotations
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...projects import ProjectError
from ...domain.errors import ReadinessError
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash
from ..presenters.summary import _render_normalization


def build_normalization_router(context: WebContext) -> APIRouter:
    """Build review, group-decision, and final-freeze HTTP actions."""

    router = APIRouter()

    @router.get("/projects/{project_id}/normalization", response_class=HTMLResponse)
    async def review_prepared_data(request: Request, project_id: str):
        require_session(request)
        return _render_normalization(request, context, project_id)

    @router.post(
        "/projects/{project_id}/normalization/groups/{group_id}/accept"
    )
    async def accept_prepared_change(
        request: Request,
        project_id: str,
        group_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.normalization.decide_group,
                project_id,
                str(form["run_id"]),
                group_id,
                approve=True,
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
            )
        except (ProjectError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared change accepted.")
        return RedirectResponse(
            f"/projects/{project_id}/normalization",
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/normalization/groups/{group_id}/reject"
    )
    async def reject_prepared_change(
        request: Request,
        project_id: str,
        group_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.normalization.decide_group,
                project_id,
                str(form["run_id"]),
                group_id,
                approve=False,
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
            )
        except (ProjectError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared change sent back for correction.")
        return RedirectResponse(
            f"/projects/{project_id}/normalization",
            status_code=303,
        )

    @router.post("/projects/{project_id}/normalization/approve")
    async def approve_prepared_data(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.normalization.approve,
                project_id,
                str(form["run_id"]),
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
            )
        except (ProjectError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared data approved. You can now compare it with Odoo.")
        return RedirectResponse(
            f"/projects/{project_id}/summary",
            status_code=303,
        )

    return router
