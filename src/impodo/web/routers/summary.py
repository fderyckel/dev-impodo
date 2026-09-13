"""Preparation summary route spanning canonical, quality, and review status."""

from __future__ import annotations
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..presenters.summary import _render_summary


def build_summary_router(context: WebContext) -> APIRouter:
    """Build the read-only project summary entry point for Stages E-G."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/summary", response_class=HTMLResponse)
    async def workspace_summary(request: Request, workspace_id: str):
        require_session(request)
        if context.preflight_jobs is not None:
            active_comparison = context.preflight_jobs.active(workspace_id)
            if active_comparison is not None:
                return RedirectResponse(
                    f"/workspaces/{workspace_id}/preflight/"
                    f"{active_comparison.job_id}",
                    status_code=303,
                )
        if context.preparation_jobs is not None:
            active = context.preparation_jobs.active(workspace_id)
            if active is not None:
                return RedirectResponse(
                    f"/workspaces/{workspace_id}/preparation/{active.job_id}",
                    status_code=303,
                )
        return await run_in_threadpool(
            _render_summary,
            request,
            context,
            workspace_id,
        )

    return router
