"""Preview and execute the practical local Odoo master-data load."""

from __future__ import annotations

import csv
from io import StringIO

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ...access import AuthorizationError
from ...connectors import ConnectorError
from ...odoo_writer import OdooWriteError
from ...odoo_readback import OdooReadbackError
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
            reconciliation=context.reconciliation.current(project_id),
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
        try:
            reader = context.readback_reader_factory(project, api_key)
            report = await run_in_threadpool(
                context.reconciliation.reconcile,
                project_id,
                expected_execution_run_id=run.run_id,
                reader=reader,
                actor=context.actor,
            )
        except (OdooReadbackError, ProjectError, WorkspaceError) as error:
            _flash(
                request,
                f"The load outcome was saved, but verification could not finish: {error}",
            )
        else:
            _flash_reconciliation(request, report)
        return RedirectResponse(f"/projects/{project_id}/load", status_code=303)

    @router.post("/projects/{project_id}/load/reconcile")
    async def reconcile_load(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "execution_run_id",
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
            reader = context.readback_reader_factory(project, api_key)
            report = await run_in_threadpool(
                context.reconciliation.reconcile,
                project_id,
                expected_execution_run_id=_text(form, "execution_run_id"),
                reader=reader,
                actor=context.actor,
            )
        except (
            AuthorizationError,
            ConnectorError,
            OdooReadbackError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return render(request, project_id, error=str(error), status_code=422)
        _flash_reconciliation(request, report)
        return RedirectResponse(f"/projects/{project_id}/load", status_code=303)

    @router.get("/projects/{project_id}/load/fallout.csv")
    async def download_fallout(request: Request, project_id: str):
        require_session(request)
        report = context.reconciliation.current(project_id)
        if report is None:
            return Response("Verification result not found", status_code=404)
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            (
                "dataset",
                "source_row",
                "odoo_model",
                "operation",
                "verification_status",
                "differing_fields",
                "message",
                "retry_safe",
            )
        )
        for row in report.rows:
            if row.status.value == "VERIFIED":
                continue
            writer.writerow(
                (
                    row.dataset,
                    row.source_row,
                    row.target_model,
                    row.operation,
                    row.status.value,
                    ";".join(row.differing_fields),
                    row.message,
                    "yes" if row.retry_safe else "no",
                )
            )
        filename = f"impodo-load-fallout-{report.execution_run_id}.csv"
        return Response(
            stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router


def _flash_reconciliation(request: Request, report) -> None:
    if report.unknown_count:
        _flash(
            request,
            f"Verification needs review: {report.unknown_count} outcome(s) remain unknown.",
        )
    elif report.fallout_count:
        _flash(
            request,
            f"Verification found {report.fallout_count} fallout row(s).",
        )
    else:
        _flash(
            request,
            f"Verified {report.verified_count} row(s) against Odoo.",
        )
