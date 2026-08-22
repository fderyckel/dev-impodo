"""Preview and execute a schema-bound Odoo load."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO
from typing import Sequence
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ...access import AuthorizationError, Capability
from ...application.execution_service import (
    ExecutionPreview,
    validated_create_batch_rows,
)
from ...connectors import ConnectorError
from ...odoo_writer import OdooWriteError
from ...odoo_readback import OdooReadbackError
from ...models import OdooReadIdentity, OdooWriteIdentity
from ...projects import WorkspaceState, OdooConnectionMode, ProjectError
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ..constants import DEFAULT_LOAD_ROWS_PER_PAGE, LOAD_ROW_PAGE_SIZES
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    store_target_credential,
)


async def _probe_remote_write_identity(
    context: WebContext,
    project: WorkspaceState,
    preview: ExecutionPreview,
    api_key: str,
) -> OdooWriteIdentity | None:
    """Probe and context-bind remote execution without exposing identifiers."""

    if project.odoo_connection_mode is not OdooConnectionMode.REMOTE:
        return None
    identity = await run_in_threadpool(
        context.write_identity_probe,
        project,
        api_key,
        preview.api_scope,
    )
    schema = context.queries.get_odoo_schema_catalog(project.project_id)
    if schema is None or not schema.read_context_hash:
        raise WorkspaceError(
            "Refresh the remote Odoo schema identity before configuring a load"
        )
    if identity.context_hash != schema.read_context_hash:
        raise WorkspaceError(
            "The write credential does not use the reviewed Odoo context"
        )
    return identity


async def _probe_current_read_identity(
    context: WebContext,
    project: WorkspaceState,
    preview: ExecutionPreview,
) -> tuple[OdooReadIdentity | None, str]:
    """Re-probe the exact comparison credential before any remote write."""

    if project.odoo_connection_mode is not OdooConnectionMode.REMOTE:
        return None, ""
    credential = get_target_credential(
        context.secret_store,
        project,
        TargetCredentialRole.READ,
    )
    if credential is None:
        raise SecretStoreError(
            "Enter the current Odoo read key, refresh the schema, and compare again"
        )
    if not preview.snapshot.readable_models:
        raise WorkspaceError("Refresh the remote Odoo schema and compare again")
    identity = await run_in_threadpool(
        context.read_identity_probe,
        project,
        credential.secret,
        preview.snapshot.readable_models,
    )
    return identity, credential.binding_hash


@dataclass(frozen=True, slots=True)
class LoadRowPage:
    """Bound the write rows rendered in one browser response."""

    rows: tuple[object, ...]
    page: int
    page_count: int
    page_size: int
    total: int
    first_row: int
    last_row: int


def _load_row_page(
    rows: Sequence[object],
    *,
    requested_page: str | None,
    requested_page_size: str | None,
) -> LoadRowPage:
    """Return one clamped 20- or 50-row page for the load review."""

    try:
        page_size = int(requested_page_size or DEFAULT_LOAD_ROWS_PER_PAGE)
    except ValueError:
        page_size = DEFAULT_LOAD_ROWS_PER_PAGE
    if page_size not in LOAD_ROW_PAGE_SIZES:
        page_size = DEFAULT_LOAD_ROWS_PER_PAGE
    try:
        page = max(1, int(requested_page or "1"))
    except ValueError:
        page = 1
    total = len(rows)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(page, page_count)
    start = (page - 1) * page_size
    page_rows = tuple(rows[start : start + page_size])
    return LoadRowPage(
        rows=page_rows,
        page=page,
        page_count=page_count,
        page_size=page_size,
        total=total,
        first_row=(start + 1 if page_rows else 0),
        last_row=start + len(page_rows),
    )


def _load_row_page_url(page: int, page_size: int) -> str:
    query = urlencode({"rows_page": page, "rows_per_page": page_size})
    return f"?{query}#row-outcomes"


def build_execution_router(context: WebContext) -> APIRouter:
    """Build the Stage-J load action and Stage-K read-back result flow."""

    router = APIRouter()

    def render(
        request: Request,
        project_id: str,
        *,
        step: str,
        error: str | None = None,
        status_code: int = 200,
    ):
        project = context.queries.get(project_id)
        preview = context.execution.current_preview(project_id)
        if preview is None:
            return RedirectResponse(
                f"/workspaces/{project_id}/summary",
                status_code=303,
            )
        if step == "confirm" and preview.current_run is not None:
            step = "outcome"
        try:
            has_stored_write_key = bool(
                get_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.WRITE,
                )
            )
        except SecretStoreError:
            has_stored_write_key = False
        reconciliation = context.reconciliation.current(project_id)
        if reconciliation is not None:
            load_rows = reconciliation.rows
        elif preview.current_run is not None:
            load_rows = preview.current_run.rows
        else:
            load_rows = ()
        load_row_page = _load_row_page(
            load_rows,
            requested_page=request.query_params.get("rows_page"),
            requested_page_size=request.query_params.get("rows_per_page"),
        )
        return _render(
            request,
            "project_load.html",
            project=project,
            preview=preview,
            reconciliation=reconciliation,
            load_step=step,
            load_row_page=load_row_page,
            load_row_page_size_options=tuple(
                {
                    "size": size,
                    "url": _load_row_page_url(1, size),
                }
                for size in LOAD_ROW_PAGE_SIZES
            ),
            load_row_previous_url=(
                _load_row_page_url(
                    load_row_page.page - 1,
                    load_row_page.page_size,
                )
                if load_row_page.page > 1
                else None
            ),
            load_row_next_url=(
                _load_row_page_url(
                    load_row_page.page + 1,
                    load_row_page.page_size,
                )
                if load_row_page.page < load_row_page.page_count
                else None
            ),
            has_stored_write_key=has_stored_write_key,
            error=error,
            status_code=status_code,
        )

    @router.get("/workspaces/{project_id}/load")
    async def load_landing(request: Request, project_id: str):
        require_session(request)
        preview = context.execution.current_preview(project_id)
        if preview is None:
            destination = f"/workspaces/{project_id}/summary"
        elif preview.current_run is not None:
            destination = f"/workspaces/{project_id}/load/outcome"
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
        else:
            destination = f"/workspaces/{project_id}/load/review"
        return RedirectResponse(destination, status_code=303)

    @router.get(
        "/workspaces/{project_id}/load/review",
        response_class=HTMLResponse,
    )
    async def review_load(request: Request, project_id: str):
        require_session(request)
        return render(request, project_id, step="review")

    @router.get(
        "/workspaces/{project_id}/load/confirm",
        response_class=HTMLResponse,
    )
    async def confirm_load(request: Request, project_id: str):
        require_session(request)
        preview = context.execution.current_preview(project_id)
        if preview is None:
            return RedirectResponse(
                f"/workspaces/{project_id}/summary",
                status_code=303,
            )
        if preview.current_run is not None:
            return RedirectResponse(
                f"/workspaces/{project_id}/load/outcome",
                status_code=303,
            )
        if not preview.can_load:
            _flash(
                request,
                preview.scope_error
                or (
                    "Resolve every row needing attention before confirming "
                    "the Odoo load."
                ),
            )
            return RedirectResponse(
                f"/workspaces/{project_id}/load/review",
                status_code=303,
            )
        return render(request, project_id, step="confirm")

    @router.get(
        "/workspaces/{project_id}/load/outcome",
        response_class=HTMLResponse,
    )
    async def review_outcome(request: Request, project_id: str):
        require_session(request)
        preview = context.execution.current_preview(project_id)
        if preview is None:
            return RedirectResponse(
                f"/workspaces/{project_id}/summary",
                status_code=303,
            )
        if preview.current_run is None:
            return RedirectResponse(
                f"/workspaces/{project_id}/load/review",
                status_code=303,
            )
        return render(request, project_id, step="outcome")

    @router.post("/workspaces/{project_id}/load")
    async def load_into_odoo(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "snapshot_hash",
                "batch_rows",
                "write_api_key",
                "remember_write_api_key",
                "api_key",
                "remember_api_key",
            },
        )
        project = context.queries.get(project_id)
        try:
            context.authorization.require(
                context.actor,
                Capability.EXPORT_PLAN_EXECUTE,
                project_id=project_id,
            )
            batch_rows = validated_create_batch_rows(_text(form, "batch_rows"))
            preview = context.execution.current_preview(project_id)
            if preview is None:
                raise WorkspaceError("Compare the prepared data with Odoo first")
            if preview.scope_error:
                raise WorkspaceError(preview.scope_error)
            read_identity, read_credential_binding_hash = (
                await _probe_current_read_identity(
                    context,
                    project,
                    preview,
                )
            )
            submitted_key = _text(form, "write_api_key") or _text(
                form,
                "api_key",
            )
            if submitted_key:
                write_identity = await _probe_remote_write_identity(
                    context,
                    project,
                    preview,
                    submitted_key,
                )
                write_credential = store_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.WRITE,
                    submitted_key,
                    persistent=(
                        "remember_write_api_key" in form
                        or "remember_api_key" in form
                    ),
                )
                audit_stored_target_credential(
                    context.projects,
                    project,
                    TargetCredentialRole.WRITE,
                    write_credential,
                    actor=context.actor,
                )
            else:
                write_credential = get_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.WRITE,
                )
            if write_credential is None:
                raise SecretStoreError(
                    "Enter a separate Odoo write API key for this exact target"
                )
            api_key = write_credential.secret
            if not submitted_key:
                write_identity = await _probe_remote_write_identity(
                    context,
                    project,
                    preview,
                    api_key,
                )
            executor = context.write_executor_factory(
                project,
                api_key,
                preview.api_scope,
            )
            run = await run_in_threadpool(
                context.execution.execute,
                project_id,
                expected_snapshot_hash=_text(form, "snapshot_hash"),
                executor=executor,
                actor=context.actor,
                batch_rows=batch_rows,
                read_identity=read_identity,
                read_credential_binding_hash=read_credential_binding_hash,
                write_identity=write_identity,
                write_credential_binding_hash=(
                    write_credential.binding_hash if write_identity is not None else ""
                ),
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
                step="confirm",
                error=str(error),
                status_code=422,
            )
        try:
            readback_identity = await _probe_remote_write_identity(
                context,
                project,
                preview,
                api_key,
            )
            reader = context.readback_reader_factory(
                project,
                api_key,
                preview.api_scope,
            )
            report = await run_in_threadpool(
                context.reconciliation.reconcile,
                project_id,
                expected_execution_run_id=run.run_id,
                reader=reader,
                actor=context.actor,
                write_identity=readback_identity,
                write_credential_binding_hash=(
                    write_credential.binding_hash
                    if readback_identity is not None
                    else ""
                ),
            )
        except (
            ConnectorError,
            OdooReadbackError,
            ProjectError,
            WorkspaceError,
        ) as error:
            _flash(
                request,
                f"The load outcome was saved, but verification could not finish: {error}",
            )
        else:
            _flash_reconciliation(request, report)
        return RedirectResponse(
            f"/workspaces/{project_id}/load/outcome",
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/load/reconcile")
    async def reconcile_load(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "execution_run_id",
                "write_api_key",
                "remember_write_api_key",
                "api_key",
                "remember_api_key",
            },
        )
        project = context.queries.get(project_id)
        try:
            context.authorization.require(
                context.actor,
                Capability.EXPORT_PLAN_EXECUTE,
                project_id=project_id,
            )
            preview = context.execution.current_preview(project_id)
            if preview is None:
                raise WorkspaceError("Compare the prepared data with Odoo first")
            submitted_key = _text(form, "write_api_key") or _text(
                form,
                "api_key",
            )
            if submitted_key:
                api_key = submitted_key
                requested_persistence = (
                    "remember_write_api_key" in form
                    or "remember_api_key" in form
                )
            else:
                write_credential = get_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.WRITE,
                )
                if write_credential is None:
                    raise SecretStoreError(
                        "Enter a separate Odoo write API key for this exact target"
                    )
                api_key = write_credential.secret
                requested_persistence = False
            write_identity = await _probe_remote_write_identity(
                context,
                project,
                preview,
                api_key,
            )
            if submitted_key:
                write_credential = store_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.WRITE,
                    submitted_key,
                    persistent=requested_persistence,
                )
                audit_stored_target_credential(
                    context.projects,
                    project,
                    TargetCredentialRole.WRITE,
                    write_credential,
                    actor=context.actor,
                )
            reader = context.readback_reader_factory(
                project,
                api_key,
                preview.api_scope,
            )
            report = await run_in_threadpool(
                context.reconciliation.reconcile,
                project_id,
                expected_execution_run_id=_text(form, "execution_run_id"),
                reader=reader,
                actor=context.actor,
                write_identity=write_identity,
                write_credential_binding_hash=(
                    write_credential.binding_hash
                    if write_identity is not None
                    else ""
                ),
            )
        except (
            AuthorizationError,
            ConnectorError,
            OdooReadbackError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return render(
                request,
                project_id,
                step="outcome",
                error=str(error),
                status_code=422,
            )
        _flash_reconciliation(request, report)
        return RedirectResponse(
            f"/workspaces/{project_id}/load/outcome",
            status_code=303,
        )

    @router.get("/workspaces/{project_id}/load/fallout.csv")
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
