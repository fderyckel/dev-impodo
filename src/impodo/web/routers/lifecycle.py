"""Lifecycle browser routes."""

from __future__ import annotations
import secrets
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from ..security import require_session
from fastapi import APIRouter
from ..context import LifecycleRouteContext
from ..forms import _secure_form
from ..diagnostics import create_diagnostic_bundle
from ..presenters.common import _render


def build_lifecycle_router(context: LifecycleRouteContext) -> APIRouter:
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

    @router.get("/health")
    async def health(request: Request):
        require_session(request)
        return JSONResponse({"status": "ok"})

    @router.post("/diagnostics/bundle")
    async def diagnostic_bundle(request: Request):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        recorder = request.app.state.diagnostic_recorder
        if recorder is None:
            raise HTTPException(
                status_code=503,
                detail="Local diagnostics are not available in this session",
            )
        recorder.record_lifecycle("diagnostic_bundle_created")
        recorder.flush()
        payload = await run_in_threadpool(
            create_diagnostic_bundle,
            recorder.path.parent,
            build_contract=request.app.state.build_contract,
        )
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    'attachment; filename="impodo-diagnostic-bundle.zip"'
                ),
            },
        )

    @router.post("/quit", response_class=HTMLResponse)
    async def quit_impodo(request: Request):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        server = request.app.state.server
        if server is not None:
            server.should_exit = True
        return _render(request, "goodbye.html")

    return router
