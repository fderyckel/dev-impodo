"""Serve static data-manager concept help without opening project state."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..presenters.common import _render
from ..security import require_session


def build_concepts_router() -> APIRouter:
    """Build the authenticated, read-only browser concepts route."""

    router = APIRouter()

    @router.get("/concepts", response_class=HTMLResponse)
    async def concepts_page(request: Request):
        require_session(request)
        return _render(request, "concepts.html")

    return router

