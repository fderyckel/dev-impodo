"""Stage 8B guarded Odoo-to-Odoo execution and read-back workflow."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.workspace.execution.load_jobs import (
    LoadJobNotFoundError,
    LoadJobResult,
    LoadJobStateError,
)
from impodo.application.workspace.execution.service import (
    execution_api_scope,
    validated_create_batch_rows,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.execution.odoo_readback import OdooReadbackError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateError,
    transfer_destination_workspace,
)

from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    get_target_credential_status,
)


def _current_evidence(context: WebContext, workspace_id: str):
    selection = context.queries.get_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if selection is None or schema is None:
        raise WorkspaceError("Freeze the Odoo source before preparing the load")
    return selection, schema


def _destination_models(selection) -> tuple[str, ...]:
    models = tuple(
        sorted(
            item.source.model
            for item in selection.datasets
            if isinstance(item.source, OdooSourceBinding)
        )
    )
    if len(models) != len(selection.datasets):
        raise WorkspaceError("Stage 8B requires frozen Odoo source tables")
    return models


def _target_server(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return parsed.netloc or parsed.path or "Configured Odoo server"


def _manager(context: WebContext):
    if context.load_jobs is None:
        raise RuntimeError("Background Odoo load jobs are unavailable")
    return context.load_jobs


def _progress_url(workspace_id: str, job_id: str) -> str:
    return f"/workspaces/{workspace_id}/transfer-load/progress/{job_id}"


def _get_job(context: WebContext, workspace_id: str, job_id: str):
    try:
        return _manager(context).get(workspace_id, job_id)
    except LoadJobNotFoundError as error:
        raise HTTPException(status_code=404, detail="Odoo transfer job not found") from error


def _job_payload(job) -> dict[str, object]:
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
        "relationship_total_count": job.relationship_total_count,
        "relationship_completed_count": job.relationship_completed_count,
        "load_group_number": job.load_group_number,
        "load_group_count": job.load_group_count,
        "not_completed_count": job.not_completed_count,
        "progress_percent": job.progress_percent,
        "execution_run_id": job.execution_run_id,
        "verification_complete": job.verification_complete,
        "completion_warning": job.completion_warning,
        "completion_warning_code": job.completion_warning_code,
        "failure_message": job.failure_message,
        "redirect_url": (
            f"/workspaces/{job.workspace_id}/transfer-load/outcome"
            if job.status.value == "SUCCEEDED"
            else ""
        ),
    }


def _dataset_rows(package, run, reconciliation):
    if package is None:
        return ()
    attempts = run.rows if run is not None else ()
    verified = reconciliation.rows if reconciliation is not None else ()
    return tuple(
        {
            "dataset": item,
            "accepted": sum(
                row.dataset == item.dataset_name
                and row.status.value in {"COMMITTED", "PARTIALLY_APPLIED"}
                for row in attempts
            ),
            "attention": sum(
                row.dataset == item.dataset_name
                and row.status.value in {"FAILED", "BLOCKED", "OUTCOME_UNKNOWN"}
                for row in attempts
            ),
            "verified": sum(
                row.dataset == item.dataset_name and row.status.value == "VERIFIED"
                for row in verified
            ),
        }
        for item in package.datasets
    )


def _render_transfer(
    request: Request,
    context: WebContext,
    workspace_id: str,
    *,
    step: str,
    error: str | None = None,
    status_code: int = 200,
):
    workspace = context.queries.get(workspace_id)
    selection, schema = _current_evidence(context, workspace_id)
    package = workspace.transfer_review_package
    report = workspace.transfer_preflight_report
    run = context.execution.current_transfer_run(workspace_id)
    reconciliation = context.reconciliation.current(workspace_id) if run else None
    snapshot = None
    if run is not None:
        try:
            snapshot = context.preflight.execution_snapshot(
                workspace_id,
                run.preflight_run_id,
            )
        except ReadinessError:
            snapshot = None
    else:
        snapshot = context.transfer_execution.current_snapshot(
            workspace,
            selection,
            schema,
        )
    current_preflight = bool(
        report
        and workspace.transfer_preflight_ready(
            source_selection_hash=selection.content_hash,
            source_schema_hash=schema.content_hash,
        )
    )
    if run is None and (package is None or report is None or not current_preflight):
        if error:
            _flash(request, error)
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-preflight",
            status_code=303,
        )
    if step == "confirm" and snapshot is None:
        step = "prepare"
    return _render(
        request,
        "workspace_transfer_load.html",
        workspace_state=workspace,
        transfer_step=step,
        transfer_review_package=package,
        transfer_preflight_report=report,
        transfer_preflight_current=current_preflight,
        transfer_snapshot=snapshot,
        transfer_run=run,
        reconciliation=reconciliation,
        transfer_dataset_rows=_dataset_rows(package, run, reconciliation),
        destination_credential_status=get_target_credential_status(
            context.secret_store,
            workspace,
            TargetCredentialRole.DESTINATION_TRANSFER,
        ),
        disable_default_read_credential_prompt=True,
        error=error,
        status_code=status_code,
    )


def build_transfer_load_router(context: WebContext) -> APIRouter:
    """Build the no-write preparation, explicit confirmation, and load routes."""

    router = APIRouter()
    matching = DestinationMatchingService(context.categorical_coverage)
    preflight = TransferPreflightService()

    @router.get("/workspaces/{workspace_id}/transfer-load")
    async def transfer_load_landing(request: Request, workspace_id: str):
        require_session(request)
        workspace = context.queries.get(workspace_id)
        if workspace.source_mode is not SourceMode.ODOO:
            return RedirectResponse(f"/workspaces/{workspace_id}/sources", status_code=303)
        active = _manager(context).active(workspace_id)
        if active is not None:
            return RedirectResponse(_progress_url(workspace_id, active.job_id), status_code=303)
        if context.execution.current_transfer_run(workspace_id) is not None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-load/outcome",
                status_code=303,
            )
        try:
            selection, schema = _current_evidence(context, workspace_id)
        except WorkspaceError:
            return RedirectResponse(f"/workspaces/{workspace_id}/sources", status_code=303)
        if not workspace.transfer_preflight_ready(
            source_selection_hash=selection.content_hash,
            source_schema_hash=schema.content_hash,
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-preflight",
                status_code=303,
            )
        snapshot = context.transfer_execution.current_snapshot(
            workspace,
            selection,
            schema,
        )
        return _render_transfer(
            request,
            context,
            workspace_id,
            step="confirm" if snapshot is not None else "prepare",
        )

    @router.post("/workspaces/{workspace_id}/transfer-load/prepare")
    async def prepare_transfer_load(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "preflight_hash"})
        workspace = context.queries.get(workspace_id)
        try:
            selection, schema = _current_evidence(context, workspace_id)
            expected_revision = _revision(form)
            if expected_revision != workspace.revision:
                raise WorkspaceStateError(
                    "The workspace changed in another request; reload before continuing"
                )
            report = workspace.transfer_preflight_report
            package = workspace.transfer_review_package
            approval = workspace.transfer_review_approval
            approved_match = workspace.destination_match_plan
            if (
                report is None
                or package is None
                or approval is None
                or approved_match is None
                or report.content_hash != _text(form, "preflight_hash")
                or not workspace.transfer_preflight_ready(
                    source_selection_hash=selection.content_hash,
                    source_schema_hash=schema.content_hash,
                )
            ):
                raise WorkspaceStateError(
                    "The destination preflight changed. Run it again before loading."
                )
            if context.execution.current_transfer_run(workspace_id) is not None:
                raise WorkspaceError(
                    "This transfer already has a load journal. Verify its outcome."
                )
            credential = get_target_credential(
                context.secret_store,
                workspace,
                TargetCredentialRole.DESTINATION_TRANSFER,
            )
            if credential is None:
                raise SecretStoreError(
                    "Return to the destination connection and enter its transfer key"
                )
            models = _destination_models(selection)
            destination = replace(
                transfer_destination_workspace(workspace),
                intended_models=models,
            )
            identity = await run_in_threadpool(
                context.read_identity_probe,
                destination,
                credential.secret,
                models,
            )
            source_origins = {}
            for dataset in selection.datasets:
                protected = await run_in_threadpool(
                    context.odoo_provenance.read_current_origins,
                    workspace_id,
                    actor=context.actor,
                    dataset_id=dataset.dataset_id,
                )
                if protected is not None:
                    source_origins[dataset.dataset_id] = protected[1]
            choices = tuple(
                DestinationMatchKeyChoice(
                    dataset_id=item.dataset_id,
                    source_column_key=item.source_column_key,
                )
                for item in approved_match.model_matches
            )
            captured_records = []

            def final_reader(*args):
                metadata, records = context.destination_match_reader(*args)
                captured_records.append(records)
                return metadata, records

            fresh_match = await run_in_threadpool(
                matching.check,
                workspace,
                selection,
                schema,
                choices,
                api_key=credential.secret,
                credential_binding_hash=credential.binding_hash,
                read_identity=identity,
                reader=final_reader,
                recorded_by=context.actor.identity.display_name,
                source_origins=source_origins,
            )
            fresh_report = preflight.build(
                workspace,
                package,
                approval,
                approved_match,
                fresh_match,
                recorded_by=context.actor.identity,
            )
            workspace = context.workspace_states.save_transfer_preflight_report(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                report=fresh_report,
            )
            if not fresh_report.ready:
                _flash(
                    request,
                    "The destination changed during the final check. Review the new preflight result before loading.",
                )
                return RedirectResponse(
                    f"/workspaces/{workspace_id}/transfer-preflight#preflight-results",
                    status_code=303,
                )
            if len(captured_records) != 1:
                raise WorkspaceError("The final destination read was incomplete")
            snapshot = await run_in_threadpool(
                context.transfer_execution.compile,
                workspace,
                selection,
                schema,
                package,
                fresh_report,
                fresh_match,
                captured_records[0],
                actor=context.actor,
            )
            await run_in_threadpool(
                context.transfer_execution.stage,
                workspace,
                selection,
                schema,
                snapshot,
            )
        except (
            AuthorizationError,
            ConnectorError,
            PermissionError,
            SecretStoreError,
            WorkspaceError,
            WorkspaceStateError,
            ValueError,
        ) as error:
            return _render_transfer(
                request,
                context,
                workspace_id,
                step="prepare",
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Final destination check passed. No Odoo data was changed. Confirm the exact load next.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-load/confirm",
            status_code=303,
        )

    @router.get(
        "/workspaces/{workspace_id}/transfer-load/confirm",
        response_class=HTMLResponse,
    )
    async def confirm_transfer_load(request: Request, workspace_id: str):
        require_session(request)
        active = _manager(context).active(workspace_id)
        if active is not None:
            return RedirectResponse(_progress_url(workspace_id, active.job_id), status_code=303)
        if context.execution.current_transfer_run(workspace_id) is not None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-load/outcome",
                status_code=303,
            )
        workspace = context.queries.get(workspace_id)
        try:
            selection, schema = _current_evidence(context, workspace_id)
        except WorkspaceError as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        if context.transfer_execution.current_snapshot(workspace, selection, schema) is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-load",
                status_code=303,
            )
        return _render_transfer(request, context, workspace_id, step="confirm")

    @router.post("/workspaces/{workspace_id}/transfer-load")
    async def execute_transfer_load(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "snapshot_hash",
                "preflight_hash",
                "batch_rows",
            },
        )
        workspace = context.queries.get(workspace_id)
        active = _manager(context).active(workspace_id)
        if active is not None:
            return RedirectResponse(_progress_url(workspace_id, active.job_id), status_code=303)
        try:
            selection, schema = _current_evidence(context, workspace_id)
            expected_revision = _revision(form)
            if workspace.revision != expected_revision:
                raise WorkspaceStateError(
                    "The workspace changed in another request; reload before continuing"
                )
            report = workspace.transfer_preflight_report
            snapshot = context.transfer_execution.current_snapshot(
                workspace,
                selection,
                schema,
            )
            if (
                report is None
                or snapshot is None
                or report.content_hash != _text(form, "preflight_hash")
                or snapshot.semantic_hash != _text(form, "snapshot_hash")
            ):
                raise WorkspaceError(
                    "The exact transfer preview changed. Prepare it again before loading."
                )
            credential = get_target_credential(
                context.secret_store,
                workspace,
                TargetCredentialRole.DESTINATION_TRANSFER,
            )
            if credential is None:
                raise SecretStoreError(
                    "Return to the destination connection and enter its transfer key"
                )
            batch_rows = validated_create_batch_rows(_text(form, "batch_rows"))
            access_context = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.EXPORT_PLAN_EXECUTE,
            )
            models = _destination_models(selection)
            target_database = snapshot.target_database
            target_server = _target_server(workspace.destination_odoo_base_url)
            project_name = context.workspace_views.get(
                workspace_id,
                actor=context.actor,
            ).migration_project.display_name

            def run_load(authorized_workspace, report_writing, report_verifying):
                if authorized_workspace != access_context:
                    raise WorkspaceError(
                        "The authorized workspace changed before the load began"
                    )
                current = context.queries.get(workspace_id)
                if current.revision != expected_revision:
                    raise WorkspaceStateError(
                        "The transfer approval changed before the load began"
                    )
                current_selection, current_schema = _current_evidence(
                    context,
                    workspace_id,
                )
                current_snapshot = context.transfer_execution.current_snapshot(
                    current,
                    current_selection,
                    current_schema,
                )
                if (
                    current_snapshot is None
                    or current_snapshot.semantic_hash != snapshot.semantic_hash
                    or current.transfer_preflight_report is None
                    or current.transfer_preflight_report.content_hash
                    != report.content_hash
                ):
                    raise WorkspaceError(
                        "The confirmed transfer changed before the load began"
                    )
                destination = replace(
                    transfer_destination_workspace(current),
                    intended_models=models,
                )
                read_identity = context.read_identity_probe(
                    destination,
                    credential.secret,
                    models,
                )
                scope = execution_api_scope(current_snapshot)
                write_identity = context.write_identity_probe(
                    destination,
                    credential.secret,
                    scope,
                )
                executor = context.write_executor_factory(
                    destination,
                    credential.secret,
                    scope,
                )
                run = context.transfer_execution.execute(
                    current,
                    current_snapshot,
                    expected_preflight_hash=report.content_hash,
                    executor=executor,
                    actor=context.actor,
                    batch_rows=batch_rows,
                    read_identity=read_identity,
                    credential_binding_hash=credential.binding_hash,
                    write_identity=write_identity,
                    progress=report_writing,
                )
                report_verifying(run)
                try:
                    verification_identity = context.write_identity_probe(
                        destination,
                        credential.secret,
                        scope,
                    )
                    reader = context.readback_reader_factory(
                        destination,
                        credential.secret,
                        scope,
                    )
                    verification = context.reconciliation.reconcile(
                        workspace_id,
                        expected_execution_run_id=run.run_id,
                        reader=reader,
                        actor=context.actor,
                        write_identity=verification_identity,
                        write_credential_binding_hash=credential.binding_hash,
                    )
                except (
                    ConnectorError,
                    OdooReadbackError,
                    ReadinessError,
                    WorkspaceError,
                    WorkspaceStateError,
                ):
                    verification_complete = False
                else:
                    verification_complete = not (
                        verification.unknown_count or verification.fallout_count
                    )
                return LoadJobResult(
                    execution_run_id=run.run_id,
                    verification_complete=verification_complete,
                )

            job = _manager(context).enqueue(
                workspace_id,
                project_name,
                target_database=target_database,
                target_server=target_server,
                target_environment="Destination",
                total_rows=snapshot.write_count,
                relationship_total_rows=sum(
                    any(field.defer_on_create for field in row.fields)
                    for row in snapshot.rows
                    if row.disposition == "CREATE"
                ),
                load_group_count=len(snapshot.relationship_plan.components),
                access_context=access_context,
                work=run_load,
            )
        except (
            AuthorizationError,
            LoadJobStateError,
            SecretStoreError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            current = context.queries.get(workspace_id)
            try:
                current_selection, current_schema = _current_evidence(
                    context,
                    workspace_id,
                )
            except WorkspaceError:
                _flash(request, str(error))
                return RedirectResponse(
                    f"/workspaces/{workspace_id}/sources",
                    status_code=303,
                )
            if not current.transfer_preflight_ready(
                source_selection_hash=current_selection.content_hash,
                source_schema_hash=current_schema.content_hash,
            ):
                _flash(request, str(error))
                return RedirectResponse(
                    f"/workspaces/{workspace_id}/transfer-preflight",
                    status_code=303,
                )
            return _render_transfer(
                request,
                context,
                workspace_id,
                step=(
                    "confirm"
                    if context.transfer_execution.current_snapshot(
                        current,
                        current_selection,
                        current_schema,
                    )
                    is not None
                    else "prepare"
                ),
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(_progress_url(workspace_id, job.job_id), status_code=303)

    @router.get(
        "/workspaces/{workspace_id}/transfer-load/progress/{job_id}",
        response_class=HTMLResponse,
    )
    async def transfer_load_progress(request: Request, workspace_id: str, job_id: str):
        require_session(request)
        job = _get_job(context, workspace_id, job_id)
        return _render(
            request,
            "workspace_transfer_load_progress.html",
            workspace_state=context.queries.get(workspace_id),
            job=job,
            job_payload=_job_payload(job),
        )

    @router.get("/workspaces/{workspace_id}/transfer-load/progress/{job_id}/status")
    async def transfer_load_progress_status(
        request: Request,
        workspace_id: str,
        job_id: str,
    ):
        require_session(request)
        return JSONResponse(_job_payload(_get_job(context, workspace_id, job_id)))

    @router.get(
        "/workspaces/{workspace_id}/transfer-load/outcome",
        response_class=HTMLResponse,
    )
    async def transfer_load_outcome(request: Request, workspace_id: str):
        require_session(request)
        active = _manager(context).active(workspace_id)
        if active is not None:
            return RedirectResponse(_progress_url(workspace_id, active.job_id), status_code=303)
        if context.execution.current_transfer_run(workspace_id) is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-load",
                status_code=303,
            )
        return _render_transfer(request, context, workspace_id, step="outcome")

    @router.post("/workspaces/{workspace_id}/transfer-load/reconcile")
    async def reconcile_transfer_load(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "execution_run_id"})
        workspace = context.queries.get(workspace_id)
        try:
            run = context.execution.current_transfer_run(workspace_id)
            if run is None or run.run_id != _text(form, "execution_run_id"):
                raise WorkspaceError("The saved transfer outcome is no longer current")
            credential = get_target_credential(
                context.secret_store,
                workspace,
                TargetCredentialRole.DESTINATION_TRANSFER,
            )
            if credential is None:
                raise SecretStoreError(
                    "Return to the destination connection and enter its transfer key"
                )
            snapshot = context.preflight.execution_snapshot(
                workspace_id,
                run.preflight_run_id,
            )
            destination = replace(
                transfer_destination_workspace(workspace),
                intended_models=snapshot.readable_models,
            )
            scope = execution_api_scope(snapshot)
            identity = await run_in_threadpool(
                context.write_identity_probe,
                destination,
                credential.secret,
                scope,
            )
            reader = context.readback_reader_factory(
                destination,
                credential.secret,
                scope,
            )
            result = await run_in_threadpool(
                context.reconciliation.reconcile,
                workspace_id,
                expected_execution_run_id=run.run_id,
                reader=reader,
                actor=context.actor,
                write_identity=identity,
                write_credential_binding_hash=credential.binding_hash,
            )
        except (
            AuthorizationError,
            ConnectorError,
            OdooReadbackError,
            ReadinessError,
            SecretStoreError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            return _render_transfer(
                request,
                context,
                workspace_id,
                step="outcome",
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Verified {result.verified_count} transfer row(s) in Odoo."
                if result.status.value == "VERIFIED"
                else "Verification found transfer rows that need review."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-load/outcome",
            status_code=303,
        )

    return router
