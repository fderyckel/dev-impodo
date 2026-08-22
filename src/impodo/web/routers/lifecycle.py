"""Lifecycle browser routes."""

from __future__ import annotations
import secrets
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _render


def build_lifecycle_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get("/launch")
    async def launch(request: Request, token: str = ""):
        if not secrets.compare_digest(token, context.launch_token):
            raise HTTPException(status_code=401, detail="Invalid launch token")
        context.launch_token = ""
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/projects", status_code=303)

    @router.get("/")
    async def root(request: Request):
        require_session(request)
        return RedirectResponse("/projects", status_code=303)

    @router.post("/quit", response_class=HTMLResponse)
    async def quit_impodo(request: Request):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        server = request.app.state.server
        if server is not None:
            server.should_exit = True
        return _render(request, "goodbye.html")

    return router
