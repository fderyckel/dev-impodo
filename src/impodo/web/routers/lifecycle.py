"""Lifecycle browser routes."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from impodo.domain.execution.models import ExecutionRowStatus
from impodo.domain.shared.access import AuthorizationError
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError

from ..composition.page_reads import run_local_operation
from ..context import LifecycleRouteContext
from ..diagnostics import create_diagnostic_bundle
from ..forms import _secure_form
from ..load_diagnostic_bundle import DiagnosticRejectionEvidence
from ..load_diagnostics import safe_uuid
from ..presenters.common import _render
from ..request_identity import request_identity
from ..security import require_session


def _diagnostic_error_selection(
    form: FormData, workspace_id: str | None, attempt_id: str | None,
) -> tuple[str, str, str] | None:
    """Accept exactly one explicitly selected saved rejection."""
    if not any(key in form for key in ("run_id", "row_id", "include_odoo_error")):
        return None
    run_id = safe_uuid(form.get("run_id"))
    row_id = form.get("row_id")
    if (
        form.get("include_odoo_error") != "1"
        or workspace_id is None
        or run_id is None
        or not isinstance(row_id, str)
        or not 1 <= len(row_id) <= 256
        or attempt_id is not None
    ):
        raise HTTPException(status_code=422, detail="Select one saved Odoo error to include")
    return workspace_id, run_id, row_id


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
        _secure_form(
            request, form,
            {"csrf_token", "attempt_id", "workspace_id", "run_id", "row_id", "include_odoo_error"},
        )
        attempt_id = safe_uuid(form.get("attempt_id"))
        if form.get("attempt_id") and attempt_id is None:
            raise HTTPException(status_code=422, detail="Invalid support reference")
        workspace_id = safe_uuid(form.get("workspace_id"))
        if form.get("workspace_id") and workspace_id is None:
            raise HTTPException(status_code=422, detail="Invalid workspace reference")
        selection = _diagnostic_error_selection(form, workspace_id, attempt_id)
        recorder = request.app.state.diagnostic_recorder
        if recorder is None:
            raise HTTPException(
                status_code=503,
                detail="Local diagnostics are not available in this session",
            )
        actor = request_identity(request, context.identity_resolver).actor

        def capture():
            rejection = None
            if selection is not None:
                selected_workspace, run_id, row_id = selection
                row, receipt = context.read_operator_error(selected_workspace, run_id, row_id, actor)
                if receipt is None or row.status not in {
                    ExecutionRowStatus.FAILED, ExecutionRowStatus.PARTIALLY_APPLIED,
                }:
                    raise HTTPException(status_code=404, detail="Saved Odoo error detail is unavailable")
                rejection = DiagnosticRejectionEvidence(selected_workspace, run_id, row, receipt)
            snapshot = context.settings_capture(workspace_id, actor) if workspace_id else None
            return snapshot, rejection

        def create(snapshot, rejection) -> bytes:
            recorder.record_lifecycle("diagnostic_bundle_created")
            recorder.flush()
            return create_diagnostic_bundle(
                recorder.path.parent,
                build_contract=request.app.state.build_contract,
                attempt_id=attempt_id,
                capture_failure_count=recorder.capture_failure_count,
                logging_costs=recorder.logging_costs_snapshot(),
                settings_snapshot=snapshot,
                rejection=rejection,
            )

        try:
            snapshot, rejection = await run_local_operation(capture)
            payload = await run_in_threadpool(create, snapshot, rejection)
        except AuthorizationError as error:
            raise HTTPException(status_code=403, detail="Workspace access is required") from error
        except WorkspaceStateNotFoundError as error:
            raise HTTPException(status_code=404, detail="Workspace not found") from error
        filename = (
            "impodo-diagnostic-bundle-with-odoo-error.zip"
            if rejection is not None else "impodo-diagnostic-bundle.zip"
        )
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
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
