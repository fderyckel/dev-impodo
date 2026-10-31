"""Preparation summary route spanning canonical, quality, and review status."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..composition.page_reads import run_page_read
from ..context import WebContext
from ..presenters.summary import _render_quality_cause, _render_summary
from ..security import require_session
from .business_expectations import build_business_expectations_router


def build_summary_router(context: WebContext) -> APIRouter:
    """Build the read-only summary of prepared and reviewed data."""

    router = APIRouter()
    router.include_router(build_business_expectations_router(context))

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
        return await run_page_read(
            _render_summary,
            request,
            context,
            workspace_id,
        )

    @router.get(
        "/workspaces/{workspace_id}/summary/quality-causes/{cause_key}",
        response_class=HTMLResponse,
    )
    async def workspace_quality_cause(
        request: Request,
        workspace_id: str,
        cause_key: str,
    ):
        require_session(request)
        return await run_page_read(
            _render_quality_cause,
            request,
            context,
            workspace_id,
            cause_key,
        )

    return router
