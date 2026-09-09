"""Expose latest-data Production setup and exact plan activation."""

from __future__ import annotations

from datetime import UTC, datetime
from secrets import compare_digest
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.application.shared.secrets import SecretStoreError
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    get_target_credential_status,
    store_target_credential,
)


def build_production_runs_router(context: WebContext) -> APIRouter:
    """Build setup-only and activation routes for one selected plan."""

    router = APIRouter()

    @router.get(
        "/projects/{project_id}/production-runs/new",
        response_class=HTMLResponse,
    )
    async def new_production_run_form(request: Request, project_id: str):
        require_session(request)
        return await run_in_threadpool(_render_new, request, context, project_id)

    @router.post("/projects/{project_id}/production-runs/new")
    async def new_production_run(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "cutover_selection_id",
                "expected_workspace_revision",
                "export_as_of",
                "label",
                "operation_id",
            },
        )
        try:
            bundle = await run_in_threadpool(context.production_runs.start_setup,
                project_id,
                expected_workspace_revision=int(
                    _text(form, "expected_workspace_revision")
                ),
                cutover_selection_id=_text(form, "cutover_selection_id"),
                label=_text(form, "label"),
                export_as_of=_text(form, "export_as_of"),
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (MigrationFoundationError, TypeError, ValueError) as error:
            return _render_new(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
                label=_text(form, "label"),
                export_as_of=_text(form, "export_as_of"),
                operation_id=_text(form, "operation_id"),
            )
        _flash(
            request,
            "Production setup is separate from Integrated Test evidence.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/production-runs/{bundle.run.migration_run_id}/fresh-data",
            status_code=303,
        )

    @router.get(
        "/projects/{project_id}/production-runs/{migration_run_id}/activate",
        response_class=HTMLResponse,
    )
    async def production_activation_form(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        require_session(request)
        return await run_in_threadpool(_render_activation,
            request,
            context,
            project_id,
            migration_run_id,
        )

    @router.post(
        "/projects/{project_id}/production-runs/{migration_run_id}/activate"
    )
    async def activate_production_run(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        form = await request.form()
        require_session(request)
        view = await run_in_threadpool(_activation_view, context, project_id, migration_run_id)
        allowed = {
            "csrf_token",
            "expected_workspace_revision",
            "operation_id",
            "remember_write_api_key",
            "write_api_key",
            "values_evidence_hash",
        }
        _secure_form(request, form, allowed)
        try:
            if (view["activation_complete"] and view["saved_operation"].operation_id == _text(form, "operation_id")):
                return RedirectResponse(f"/projects/{project_id}/runs/{migration_run_id}", status_code=303)
            if view["activation_pending"] or view["activation_complete"]:
                raise MigrationFoundationError("Continue the saved Production setup from its current step")
            if int(_text(form, "expected_workspace_revision")) != view["project"].optimistic_revision:
                raise MigrationFoundationError("The Project changed. Reload and review Production setup.")
            if _text(form, "values_evidence_hash") != view["value_review"].evidence_hash:
                raise MigrationFoundationError("Production setup changed. Reload and review its current values.")
            if not view["run_value_plan"].ready_to_continue:
                raise MigrationFoundationError("Confirm the Recipe details on Fresh data before continuing")
            values = context.production_runs.values.activation_values(
                view["binding"], view["value_review"], None, None)
            setup_state = view["setup_state"]
            target_schema, target_references = await run_in_threadpool(
                context.run_planning.target_evidence_from_workspace,
                    project_id,
                    setup_state.workspace_id,
                    actor=context.actor,
            )
            read_credential = get_target_credential(
                context.secret_store,
                setup_state,
                TargetCredentialRole.READ,
            )
            if read_credential is None:
                raise SecretStoreError(
                    "Enter and verify the Production read-only Odoo key first"
                )
            write_key = _text(form, "write_api_key")
            if not write_key:
                current_write = get_target_credential(
                    context.secret_store,
                    setup_state,
                    TargetCredentialRole.WRITE,
                )
                if current_write is None:
                    raise SecretStoreError(
                        "Enter a separate Production write API key"
                    )
                write_key = current_write.secret
            if compare_digest(read_credential.secret, write_key):
                raise SecretStoreError(
                    "Use a different Production API key for write access"
                )
            scope = context.production_runs.write_scope(migration_run_id)
            write_identity = await run_in_threadpool(
                context.write_identity_probe,
                setup_state,
                write_key,
                scope,
            )
            if _text(form, "write_api_key"):
                write_credential = store_target_credential(
                    context.secret_store,
                    setup_state,
                    TargetCredentialRole.WRITE,
                    write_key,
                    persistent="remember_write_api_key" in form,
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    setup_state,
                    TargetCredentialRole.WRITE,
                    write_credential,
                    actor=context.actor,
                )
            else:
                write_credential = current_write
            await run_in_threadpool(context.production_runs.activate,
                project_id,
                migration_run_id,
                expected_workspace_revision=int(
                    _text(form, "expected_workspace_revision")
                ),
                target_schema=target_schema,
                target_reference_bundle=target_references,
                read_credential_generation=read_credential.binding_hash,
                write_identity=write_identity,
                write_credential_generation=write_credential.binding_hash,
                parameter_values=values.parameters,
                control_values=values.controls,
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (
            ConnectorError,
            MigrationFoundationError,
            SecretStoreError,
            TypeError,
            ValueError,
        ) as error:
            return await run_in_threadpool(_render_activation,
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                operation_id=_text(form, "operation_id"),
            )
        _flash(
            request,
            "Production applications are ready for fresh comparison and approval.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}",
            status_code=303,
        )

    @router.post("/projects/{project_id}/production-runs/{migration_run_id}/activate/resume")
    async def resume_production_activation(request: Request, project_id: str, migration_run_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            await run_in_threadpool(
                context.production_runs.resume_activation, project_id, migration_run_id, actor=context.actor,
            )
        except (MigrationFoundationError, ValueError) as error:
            return await run_in_threadpool(
                _render_activation, request, context, project_id, migration_run_id,
                error=str(error), status_code=422,
            )
        _flash(request, "Production setup is complete. Continue with fresh review and load.")
        return RedirectResponse(f"/projects/{project_id}/runs/{migration_run_id}", status_code=303)

    return router


def _render_new(
    request,
    context,
    project_id,
    *,
    error=None,
    status_code=200,
    label="Production rollout",
    export_as_of=None,
    operation_id=None,
):
    project = context.migration_projects.get(project_id, actor=context.actor)
    selection = context.cutover_plans.repository.current_selection(project_id)
    qualification = (
        context.cutover_plans.repository.get_qualification(
            selection.qualification_id
        )
        if selection is not None
        else None
    )
    return _render(
        request,
        "project_production_run_new.html",
        project=project,
        selection=selection,
        qualification=qualification,
        label=label,
        export_as_of=(
            export_as_of
            if export_as_of is not None
            else datetime.now(UTC).astimezone().date().isoformat()
        ),
        operation_id=operation_id or str(uuid4()),
        error=error,
        status_code=status_code,
    )


def _render_activation(
    request,
    context,
    project_id,
    migration_run_id,
    *,
    error=None,
    status_code=200,
    operation_id=None,
):
    view = _activation_view(context, project_id, migration_run_id)
    return _render(
        request,
        "project_production_activation.html",
        **view,
        workspace_state=view["setup_state"],
        operation_id=operation_id or str(uuid4()),
        error=error,
        status_code=status_code,
    )


def _activation_view(context, project_id, migration_run_id):
    project = context.migration_projects.get(project_id, actor=context.actor)
    binding = context.production_runs.production_runs.get(migration_run_id)
    if binding.project_id != project_id:
        raise MigrationFoundationError(
            "Production run does not belong to this Project"
        )
    run = context.migration_runs.get(migration_run_id, actor=context.actor)
    data_version = context.data_versions.get(binding.data_version_id, actor=context.actor)
    setup_workspace = context.migration_workspaces.get(
        binding.setup_workspace_id,
        actor=context.actor,
    )
    setup_state = context.workspace_states.repository.get(binding.setup_workspace_id)
    plan = context.cutover_plans.repository.get_revision(
        binding.cutover_plan_id,
        binding.cutover_plan_revision,
    )
    value_review = context.production_runs.values.review(binding, plan, data_version, actor=context.actor)
    operation = context.production_runs.activation_operation(migration_run_id, actor=context.actor)
    activation_pending = operation is not None and operation.state.value != "COMMITTED"
    read_status = get_target_credential_status(
        context.secret_store,
        setup_state,
        TargetCredentialRole.READ,
    )
    write_status = get_target_credential_status(
        context.secret_store,
        setup_state,
        TargetCredentialRole.WRITE,
    )
    target_ready = False
    target_error = "Capture the Production Odoo 19 fields and supporting lists."
    try:
        context.run_planning.target_evidence_from_workspace(
            project_id,
            setup_workspace.workspace_id,
            actor=context.actor,
        )
    except MigrationFoundationError as error:
        target_error = str(error)
    else:
        target_ready = True
        target_error = ""
    if data_version.state.value != "FROZEN" or not value_review.values.ready_to_continue:
        setup_destination = f"/projects/{project_id}/production-runs/{migration_run_id}/fresh-data"
        setup_action_label = "Continue fresh data"
    elif not target_ready:
        setup_destination = f"/projects/{project_id}/runs/{migration_run_id}/odoo"
        setup_action_label = "Continue Odoo check"
    else:
        setup_destination = (
            f"/projects/{project_id}/production-runs/{migration_run_id}/activate"
        )
        setup_action_label = "Review Production setup"
    return {
        "binding": binding,
        "value_review": value_review,
        "run_value_plan": value_review.values,
        "fresh_data_complete": data_version.state.value == "FROZEN" and value_review.values.ready_to_continue,
        "activation_pending": activation_pending,
        "activation_resumable": bool(operation and isinstance(operation.detail.get("activation_inputs"), dict)
                                     and operation.detail["activation_inputs"].get("contract_version") == 1),
        "activation_complete": binding.state.value == "ACTIVE" and not activation_pending,
        "saved_operation": operation,
        "data_version": data_version,
        "plan": plan,
        "project": project,
        "read_status": read_status,
        "run": run,
        "setup_state": setup_state,
        "setup_action_label": setup_action_label,
        "setup_destination": setup_destination,
        "setup_workspace": setup_workspace,
        "target_error": target_error,
        "target_ready": target_ready,
        "write_status": write_status,
    }
