"""Summary browser routes."""

from __future__ import annotations
from fastapi import Request
from fastapi.responses import HTMLResponse
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..presenters.summary import _render_summary


def build_summary_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{project_id}/summary", response_class=HTMLResponse)
    async def project_summary(request: Request, project_id: str):
        require_session(request)
        return _render_summary(request, context, project_id)

    return router
