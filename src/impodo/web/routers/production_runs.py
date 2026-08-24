"""Expose latest-data Production setup and exact plan activation."""

from __future__ import annotations

from secrets import compare_digest
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...connectors import ConnectorError
from ...migration_foundation import MigrationFoundationError
from ...workspace_state import SourceMode
from ...secrets import SecretStoreError
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
        return _render_new(request, context, project_id)

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
            bundle = context.production_runs.start_setup(
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
        setup_state = context.workspace_states.repository.get(
            bundle.setup_workspace.workspace_id
        )
        destination = (
            "files" if setup_state.source_mode is SourceMode.FILE else "target"
        )
        _flash(
            request,
            "Production setup is separate from Integrated Test evidence.",
        )
        return RedirectResponse(
            f"/workspaces/{bundle.setup_workspace.workspace_id}/{destination}",
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
        return _render_activation(
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
        view = _activation_view(context, project_id, migration_run_id)
        allowed = {
            "csrf_token",
            "expected_workspace_revision",
            "operation_id",
            "remember_write_api_key",
            "write_api_key",
        } | {item["field_name"] for item in (*view["parameters"], *view["controls"])}
        _secure_form(request, form, allowed)
        try:
            setup_state = view["setup_state"]
            target_schema, target_references = (
                context.run_planning.target_evidence_from_workspace(
                    project_id,
                    setup_state.workspace_id,
                    actor=context.actor,
                )
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
            parameter_values = _submitted_values(form, view["parameters"])
            control_values = _submitted_values(form, view["controls"])
            context.production_runs.activate(
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
                parameter_values=parameter_values,
                control_values=control_values,
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
            return _render_activation(
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

    return router


def _render_new(
    request,
    context,
    project_id,
    *,
    error=None,
    status_code=200,
    label="Production rollout",
    export_as_of="",
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
        export_as_of=export_as_of,
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
    recipes = {
        item.recipe_id: item
        for item in context.recipes.list(project_id, actor=context.actor)
    }
    parameters = []
    controls = []
    for recipe_index, selection in enumerate(plan.selected_revisions):
        envelope = context.recipes.read_revision(
            selection.recipe_id,
            selection.recipe_revision,
            actor=context.actor,
        )
        definition = dict(envelope["recipe"])
        recipe = recipes[selection.recipe_id]
        for index, item in enumerate(
            dict(definition.get("parameter_definitions", {})).get("parameters", ())
        ):
            parameters.append(
                {
                    "definition": dict(item),
                    "field_name": f"parameter_{recipe_index}_{index}",
                    "recipe_id": selection.recipe_id,
                    "recipe_name": recipe.display_name,
                }
            )
        for index, item in enumerate(
            dict(definition.get("control_definitions", {})).get("controls", ())
        ):
            controls.append(
                {
                    "definition": dict(item),
                    "field_name": f"control_{recipe_index}_{index}",
                    "recipe_id": selection.recipe_id,
                    "recipe_name": recipe.display_name,
                }
            )
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
    return {
        "binding": binding,
        "controls": tuple(controls),
        "data_version": data_version,
        "parameters": tuple(parameters),
        "plan": plan,
        "project": project,
        "read_status": read_status,
        "run": run,
        "setup_state": setup_state,
        "setup_workspace": setup_workspace,
        "target_error": target_error,
        "target_ready": target_ready,
        "write_status": write_status,
    }


def _submitted_values(form, definitions):
    result: dict[str, dict[str, object]] = {}
    for item in definitions:
        value = _text(form, item["field_name"])
        logical_id = str(
            item["definition"].get("logical_parameter_id")
            or item["definition"].get("logical_control_id")
            or ""
        )
        if value or item["definition"].get("required"):
            result.setdefault(item["recipe_id"], {})[logical_id] = value
    return result

