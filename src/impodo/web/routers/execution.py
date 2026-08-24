"""Preview and execute a schema-bound Odoo load."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO
from secrets import compare_digest
from types import SimpleNamespace
from typing import Sequence
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ...access import AuthorizationError, Capability
from ...application.execution_service import (
    ExecutionPreview,
    validated_create_batch_rows,
)
from ...application.load_job_service import (
    LoadJobNotFoundError,
    LoadJobResult,
    LoadJobStateError,
)
from ...connectors import ConnectorError
from ...odoo_readback import OdooReadbackError
from ...models import OdooReadIdentity, OdooWriteIdentity
from ...load_jobs import LoadJob, LoadJobStatus
from ...migration_foundation import MigrationConflictError
from ...migration_production import ProductionRunError
from ...workspace_state import WorkspaceState, OdooConnectionMode, WorkspaceStateError
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ...workspace_access import WorkspaceAccessContext
from ..constants import DEFAULT_LOAD_ROWS_PER_PAGE, LOAD_ROW_PAGE_SIZES
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.navigation import build_load_workspace_navigation
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    store_target_credential,
)


def _probe_remote_write_identity_sync(
    context: WebContext,
    workspace_state: WorkspaceState,
    preview: ExecutionPreview,
    api_key: str,
) -> OdooWriteIdentity | None:
    """Probe and context-bind remote execution without exposing identifiers."""

    if workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE:
        return None
    identity = context.write_identity_probe(workspace_state, api_key, preview.api_scope)
    schema = context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
    if schema is None or not schema.read_context_hash:
        raise WorkspaceError(
            "Refresh the remote Odoo schema identity before configuring a load"
        )
    if identity.context_hash != schema.read_context_hash:
        raise WorkspaceError(
            "The write credential does not use the reviewed Odoo context"
        )
    return identity


async def _probe_remote_write_identity(
    context: WebContext,
    workspace_state: WorkspaceState,
    preview: ExecutionPreview,
    api_key: str,
) -> OdooWriteIdentity | None:
    return await run_in_threadpool(
        _probe_remote_write_identity_sync,
        context,
        workspace_state,
        preview,
        api_key,
    )


def _probe_read_identity_sync(
    context: WebContext,
    workspace_state: WorkspaceState,
    preview: ExecutionPreview,
    api_key: str,
) -> OdooReadIdentity | None:
    if workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE:
        return None
    if not preview.snapshot.readable_models:
        raise WorkspaceError("Refresh the remote Odoo schema and compare again")
    return context.read_identity_probe(
        workspace_state,
        api_key,
        preview.snapshot.readable_models,
    )


async def _probe_current_read_identity(
    context: WebContext,
    workspace_state: WorkspaceState,
    preview: ExecutionPreview,
) -> tuple[OdooReadIdentity | None, str, str | None]:
    """Re-probe the exact comparison credential before any remote write."""

    if workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE:
        return None, "", None
    credential_owner = context.production_runs.credential_workspace(
        workspace_state.workspace_id,
        actor=context.actor,
    )
    credential = get_target_credential(
        context.secret_store,
        credential_owner,
        TargetCredentialRole.READ,
    )
    if credential is None:
        raise SecretStoreError(
            "Enter the current Odoo read key, refresh the schema, and compare again"
        )
    identity = await run_in_threadpool(
        _probe_read_identity_sync,
        context,
        workspace_state,
        preview,
        credential.secret,
    )
    return identity, credential.binding_hash, credential.secret


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


def _load_progress_url(workspace_id: str, job_id: str) -> str:
    return f"/workspaces/{workspace_id}/load/progress/{job_id}"


def _target_server(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return parsed.netloc or parsed.path or "Configured Odoo server"


def _load_job_display_context(
    context: WebContext,
    workspace_id: str,
) -> tuple[str, str]:
    workspace = context.migration_workspaces.get(workspace_id, actor=context.actor)
    data_version = context.data_versions.get(
        workspace.data_version_id,
        actor=context.actor,
    )
    migration_project = context.migration_projects.get(
        workspace.project_id,
        actor=context.actor,
    )
    environment = {
        "PRODUCTION": "Production",
        "TEST": "Test",
    }.get(data_version.purpose.value, "Target")
    return migration_project.display_name, environment


def build_execution_router(context: WebContext) -> APIRouter:
    """Build the Stage-J load action and Stage-K read-back result flow."""

    router = APIRouter()

    def render(
        request: Request,
        workspace_id: str,
        *,
        step: str,
        error: str | None = None,
        status_code: int = 200,
    ):
        workspace_state = context.queries.get(workspace_id)
        credential_owner = context.production_runs.credential_workspace(
            workspace_id,
            actor=context.actor,
        )
        preview = context.execution.current_preview(workspace_id)
        if preview is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/summary",
                status_code=303,
            )
        if step == "confirm" and preview.current_run is not None:
            step = "outcome"
        try:
            has_stored_write_key = bool(
                get_target_credential(
                    context.secret_store,
                    credential_owner,
                    TargetCredentialRole.WRITE,
                )
            )
        except SecretStoreError:
            has_stored_write_key = False
        reconciliation = context.reconciliation.current(workspace_id)
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
            "workspace_load.html",
            workspace_state=workspace_state,
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

    @router.get("/workspaces/{workspace_id}/load")
    async def load_landing(request: Request, workspace_id: str):
        require_session(request)
        active_job = _manager(context).active(workspace_id)
        if active_job is not None:
            return RedirectResponse(
                _load_progress_url(workspace_id, active_job.job_id),
                status_code=303,
            )
        preview = context.execution.current_preview(workspace_id)
        if preview is None:
            destination = f"/workspaces/{workspace_id}/summary"
        elif preview.current_run is not None:
            destination = f"/workspaces/{workspace_id}/load/outcome"
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
        else:
            destination = f"/workspaces/{workspace_id}/load/review"
        return RedirectResponse(destination, status_code=303)

    @router.get(
        "/workspaces/{workspace_id}/load/review",
        response_class=HTMLResponse,
    )
    async def review_load(request: Request, workspace_id: str):
        require_session(request)
        active_job = _manager(context).active(workspace_id)
        if active_job is not None:
            return RedirectResponse(
                _load_progress_url(workspace_id, active_job.job_id),
                status_code=303,
            )
        return render(request, workspace_id, step="review")

    @router.get(
        "/workspaces/{workspace_id}/load/confirm",
        response_class=HTMLResponse,
    )
    async def confirm_load(request: Request, workspace_id: str):
        require_session(request)
        active_job = _manager(context).active(workspace_id)
        if active_job is not None:
            return RedirectResponse(
                _load_progress_url(workspace_id, active_job.job_id),
                status_code=303,
            )
        preview = context.execution.current_preview(workspace_id)
        if preview is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/summary",
                status_code=303,
            )
        if preview.current_run is not None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/load/outcome",
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
                f"/workspaces/{workspace_id}/load/review",
                status_code=303,
            )
        try:
            context.cutover_plans.assert_application_can_execute(
                workspace_id,
                actor=context.actor,
            )
        except MigrationConflictError as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/load/review",
                status_code=303,
            )
        return render(request, workspace_id, step="confirm")

    @router.get(
        "/workspaces/{workspace_id}/load/outcome",
        response_class=HTMLResponse,
    )
    async def review_outcome(request: Request, workspace_id: str):
        require_session(request)
        active_job = _manager(context).active(workspace_id)
        if active_job is not None:
            return RedirectResponse(
                _load_progress_url(workspace_id, active_job.job_id),
                status_code=303,
            )
        preview = context.execution.current_preview(workspace_id)
        if preview is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/summary",
                status_code=303,
            )
        if preview.current_run is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/load/review",
                status_code=303,
            )
        return render(request, workspace_id, step="outcome")

    @router.get(
        "/workspaces/{workspace_id}/load/progress/{job_id}",
        response_class=HTMLResponse,
    )
    async def load_progress(request: Request, workspace_id: str, job_id: str):
        require_session(request)
        job = _get_job(context, workspace_id, job_id)
        workspace = context.migration_workspaces.get(workspace_id, actor=context.actor)
        data_version = context.data_versions.get(
            workspace.data_version_id,
            actor=context.actor,
        )
        workspace_state = SimpleNamespace(
            workspace_id=job.workspace_id,
            name=job.migration_project_name,
            registered_at=True,
        )
        return _render(
            request,
            "workspace_load_progress.html",
            workspace_state=workspace_state,
            workspace_navigation=build_load_workspace_navigation(job),
            migration_context={
                "project_id": workspace.project_id,
                "data_version_id": data_version.data_version_id,
                "data_version_number": data_version.version_number,
                "data_version_purpose": data_version.purpose.value,
                "migration_run_id": workspace.migration_run_id,
                "workspace_id": workspace.workspace_id,
            },
            job=job,
            job_payload=_job_payload(job),
            status_code=200,
        )

    @router.get("/workspaces/{workspace_id}/load/progress/{job_id}/status")
    async def load_progress_status(
        request: Request,
        workspace_id: str,
        job_id: str,
    ):
        require_session(request)
        return JSONResponse(_job_payload(_get_job(context, workspace_id, job_id)))

    @router.post("/workspaces/{workspace_id}/load")
    async def load_into_odoo(request: Request, workspace_id: str):
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
        workspace_state = context.queries.get(workspace_id)
        active_job = _manager(context).active(workspace_id)
        if active_job is not None:
            return RedirectResponse(
                _load_progress_url(workspace_id, active_job.job_id),
                status_code=303,
            )
        credential_owner = context.production_runs.credential_workspace(
            workspace_id,
            actor=context.actor,
        )
        try:
            access_context = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.EXPORT_PLAN_EXECUTE,
            )
            context.cutover_plans.assert_application_can_execute(
                workspace_id,
                actor=context.actor,
            )
            batch_rows = validated_create_batch_rows(_text(form, "batch_rows"))
            preview = context.execution.current_preview(workspace_id)
            if preview is None:
                raise WorkspaceError("Compare the prepared data with Odoo first")
            if preview.scope_error:
                raise WorkspaceError(preview.scope_error)
            snapshot_hash = _text(form, "snapshot_hash")
            if snapshot_hash != preview.snapshot.semantic_hash:
                raise WorkspaceError("The load preview changed. Review it again.")
            read_credential = None
            if workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE:
                read_credential = get_target_credential(
                    context.secret_store,
                    credential_owner,
                    TargetCredentialRole.READ,
                )
                if read_credential is None:
                    raise SecretStoreError(
                        "Enter the current Odoo read key, refresh the schema, "
                        "and compare again"
                    )
                if not preview.snapshot.readable_models:
                    raise WorkspaceError(
                        "Refresh the remote Odoo schema and compare again"
                    )
            submitted_key = _text(form, "write_api_key") or _text(
                form,
                "api_key",
            )
            saved_write_credential = get_target_credential(
                context.secret_store,
                credential_owner,
                TargetCredentialRole.WRITE,
            )
            if not submitted_key and saved_write_credential is None:
                raise SecretStoreError(
                    "Enter an Odoo API key approved for loading on this exact target"
                )
            api_key = (
                submitted_key
                if submitted_key
                else saved_write_credential.secret
            )
            if (
                credential_owner.workspace_id != workspace_state.workspace_id
                and read_credential is not None
                and compare_digest(read_credential.secret, api_key)
            ):
                raise SecretStoreError(
                    "Use a different Odoo API key for write access"
                )
            remember_write_key = (
                "remember_write_api_key" in form or "remember_api_key" in form
            )

            def run_load(
                authorized_workspace: WorkspaceAccessContext,
                report_writing,
                report_verifying,
            ) -> LoadJobResult:
                if authorized_workspace != access_context:
                    raise MigrationConflictError(
                        "The authorized workspace changed before the load began"
                    )
                read_identity = _probe_read_identity_sync(
                    context,
                    workspace_state,
                    preview,
                    read_credential.secret if read_credential is not None else "",
                )
                write_identity = _probe_remote_write_identity_sync(
                    context,
                    workspace_state,
                    preview,
                    api_key,
                )
                write_credential = saved_write_credential
                if submitted_key:
                    write_credential = store_target_credential(
                        context.secret_store,
                        credential_owner,
                        TargetCredentialRole.WRITE,
                        submitted_key,
                        persistent=remember_write_key,
                    )
                    audit_stored_target_credential(
                        context.workspace_states,
                        credential_owner,
                        TargetCredentialRole.WRITE,
                        write_credential,
                        actor=context.actor,
                    )
                if write_credential is None:
                    raise SecretStoreError(
                        "Enter an Odoo API key approved for loading on this exact target"
                    )
                read_credential_binding_hash = (
                    read_credential.binding_hash
                    if read_credential is not None
                    else ""
                )
                context.production_runs.assert_execution_authority(
                    workspace_id,
                    read_identity=read_identity,
                    read_credential_generation=read_credential_binding_hash,
                    expected_read_credential_generation=(
                        preview.snapshot.read_credential_binding_hash
                    ),
                    write_identity=write_identity,
                    write_credential_generation=write_credential.binding_hash,
                    actor=context.actor,
                )
                executor = context.write_executor_factory(
                    workspace_state,
                    api_key,
                    preview.api_scope,
                )
                run = context.execution.execute(
                    workspace_id,
                    expected_snapshot_hash=snapshot_hash,
                    executor=executor,
                    actor=context.actor,
                    batch_rows=batch_rows,
                    read_identity=read_identity,
                    read_credential_binding_hash=read_credential_binding_hash,
                    write_identity=write_identity,
                    write_credential_binding_hash=(
                        write_credential.binding_hash
                        if write_identity is not None
                        else ""
                    ),
                    progress=report_writing,
                )
                report_verifying(run)
                try:
                    readback_identity = _probe_remote_write_identity_sync(
                        context,
                        workspace_state,
                        preview,
                        api_key,
                    )
                    reader = context.readback_reader_factory(
                        workspace_state,
                        api_key,
                        preview.api_scope,
                    )
                    context.reconciliation.reconcile(
                        workspace_id,
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
                    WorkspaceStateError,
                    WorkspaceError,
                ):
                    verification_complete = False
                else:
                    verification_complete = True
                return LoadJobResult(
                    execution_run_id=run.run_id,
                    verification_complete=verification_complete,
                )

            migration_project_name, target_environment = _load_job_display_context(
                context,
                workspace_id,
            )
            job = _manager(context).enqueue(
                workspace_id,
                migration_project_name,
                target_database=preview.snapshot.target_database,
                target_server=_target_server(workspace_state.odoo_base_url),
                target_environment=target_environment,
                total_rows=preview.snapshot.write_count,
                access_context=access_context,
                work=run_load,
            )
        except (
            AuthorizationError,
            LoadJobStateError,
            MigrationConflictError,
            ProductionRunError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return render(
                request,
                workspace_id,
                step="confirm",
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            _load_progress_url(workspace_id, job.job_id),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/load/reconcile")
    async def reconcile_load(request: Request, workspace_id: str):
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
        active_job = _manager(context).active(workspace_id)
        if active_job is not None:
            return RedirectResponse(
                _load_progress_url(workspace_id, active_job.job_id),
                status_code=303,
            )
        workspace_state = context.queries.get(workspace_id)
        credential_owner = context.production_runs.credential_workspace(
            workspace_id,
            actor=context.actor,
        )
        try:
            context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.EXPORT_PLAN_EXECUTE,
            )
            preview = context.execution.current_preview(workspace_id)
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
                    credential_owner,
                    TargetCredentialRole.WRITE,
                )
                if write_credential is None:
                    raise SecretStoreError(
                        "Enter an Odoo API key approved for loading on this exact target"
                    )
                api_key = write_credential.secret
                requested_persistence = False
            write_identity = await _probe_remote_write_identity(
                context,
                workspace_state,
                preview,
                api_key,
            )
            if submitted_key:
                write_credential = store_target_credential(
                    context.secret_store,
                    credential_owner,
                    TargetCredentialRole.WRITE,
                    submitted_key,
                    persistent=requested_persistence,
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    credential_owner,
                    TargetCredentialRole.WRITE,
                    write_credential,
                    actor=context.actor,
                )
            reader = context.readback_reader_factory(
                workspace_state,
                api_key,
                preview.api_scope,
            )
            report = await run_in_threadpool(
                context.reconciliation.reconcile,
                workspace_id,
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
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return render(
                request,
                workspace_id,
                step="outcome",
                error=str(error),
                status_code=422,
            )
        _flash_reconciliation(request, report)
        return RedirectResponse(
            f"/workspaces/{workspace_id}/load/outcome",
            status_code=303,
        )

    @router.get("/workspaces/{workspace_id}/load/fallout.csv")
    async def download_fallout(request: Request, workspace_id: str):
        require_session(request)
        report = context.reconciliation.current(workspace_id)
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


def _job_payload(job: LoadJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "phase": job.phase.value,
        "message": job.message,
        "target_database": job.target_database,
        "target_server": job.target_server,
        "target_environment": job.target_environment,
        "completed_rows": job.completed_rows,
        "total_rows": job.total_rows,
        "created_count": job.created_count,
        "updated_count": job.updated_count,
        "attention_count": job.attention_count,
        "relationship_pending_count": job.relationship_pending_count,
        "not_attempted_count": job.not_attempted_count,
        "progress_percent": job.progress_percent,
        "execution_run_id": job.execution_run_id,
        "verification_complete": job.verification_complete,
        "failure_message": job.failure_message,
        "redirect_url": (
            f"/workspaces/{job.workspace_id}/load/outcome"
            if job.status is LoadJobStatus.SUCCEEDED
            else ""
        ),
    }


def _get_job(context: WebContext, workspace_id: str, job_id: str) -> LoadJob:
    try:
        return _manager(context).get(workspace_id, job_id)
    except LoadJobNotFoundError as error:
        raise HTTPException(status_code=404, detail="Odoo load job not found") from error


def _manager(context: WebContext):
    if context.load_jobs is None:
        raise RuntimeError("Background Odoo load jobs are unavailable")
    return context.load_jobs
