"""Preview and execute the practical local Odoo master-data load."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...access import AuthorizationError
from ...connectors import ConnectorError
from ...odoo_writer import OdooWriteError
from ...projects import ProjectError
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_readers import _target_credential_id


def build_execution_router(context: WebContext) -> APIRouter:
    """Build the single preview and explicit load action for Stage J."""

    router = APIRouter()

    def render(
        request: Request,
        project_id: str,
        *,
        error: str | None = None,
        status_code: int = 200,
    ):
        project = context.queries.get(project_id)
        preview = context.execution.current_preview(project_id)
        if preview is None:
            return RedirectResponse(
                f"/projects/{project_id}/summary",
                status_code=303,
            )
        try:
            has_stored_key = bool(
                context.secret_store.get(_target_credential_id(project))
            )
        except SecretStoreError:
            has_stored_key = False
        return _render(
            request,
            "project_load.html",
            project=project,
            preview=preview,
            has_stored_key=has_stored_key,
            error=error,
            status_code=status_code,
        )

    @router.get("/projects/{project_id}/load", response_class=HTMLResponse)
    async def preview_load(request: Request, project_id: str):
        require_session(request)
        return render(request, project_id)

    @router.post("/projects/{project_id}/load")
    async def load_into_odoo(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "snapshot_hash",
                "api_key",
                "remember_api_key",
            },
        )
        project = context.queries.get(project_id)
        try:
            credential_id = _target_credential_id(project)
            submitted_key = _text(form, "api_key")
            if submitted_key:
                context.secret_store.set(
                    credential_id,
                    submitted_key,
                    persistent="remember_api_key" in form,
                )
            api_key = submitted_key or context.secret_store.get(credential_id) or ""
            executor = context.write_executor_factory(project, api_key)
            run = await run_in_threadpool(
                context.execution.execute,
                project_id,
                expected_snapshot_hash=_text(form, "snapshot_hash"),
                executor=executor,
                actor=context.actor,
            )
        except (
            AuthorizationError,
            ConnectorError,
            OdooWriteError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return render(
                request,
                project_id,
                error=str(error),
                status_code=422,
            )
        if run.unknown_count:
            _flash(
                request,
                "The Odoo response was lost. Impodo stopped without retrying; "
                "review the saved outcome below.",
            )
        elif run.failed_count:
            _flash(
                request,
                "The load finished with rows that Odoo did not accept. "
                "Review the saved outcome below.",
            )
        else:
            _flash(request, f"Odoo accepted {run.committed_count} row(s).")
        return RedirectResponse(f"/projects/{project_id}/load", status_code=303)

    return router
