"""Read sealed stage evidence and start an explicitly requested working revision.

Historical GETs use bounded local projections and never run authoring setup,
prepare data, refresh Odoo, or create a successor. Protected evidence access is
resolved before opening any child store. The successor command reuses the
existing exact-target correction owner.
"""

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import SchemaOrigin
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.models import MigrationWorkspaceState

from ..composition.page_reads import run_local_operation, run_page_read
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..request_identity import request_identity
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)
from .schema import _capture_selected_schema

STAGE_LABELS = (
    "Source data",
    "Odoo data",
    "Match data",
    "Prepare data",
    "Final review",
    "Load into Odoo",
)


def build_history_router(context: WebContext) -> APIRouter:
    """Expose immutable saved views independently of editable authoring pages."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/history/{stage}")
    async def saved_stage(request: Request, workspace_id: str, stage: int):
        require_session(request)
        if not 1 <= stage <= 6:
            raise HTTPException(404, "Saved stage not found")
        return await run_page_read(_saved_stage, request, context, workspace_id, stage)

    @router.post("/projects/{project_id}/completed-loads/{workspace_id}/continue")
    async def continue_editing(request: Request, project_id: str, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "request_id"})
        identity = request_identity(request, context.identity_resolver)
        actor = identity.actor
        try:

            def start():
                context.project_queries.get_visible_project(
                    project_id,
                    scope=identity.organization_scope,
                    authorization=identity.authorization,
                )
                workspace = context.migration_workspaces.get(workspace_id, actor=actor)
                if workspace.project_id != project_id:
                    raise HTTPException(404, "Saved load not found")
                return context.corrections.start(
                    workspace_id, actor=actor, request_id=_text(form, "request_id")
                )

            successor = await run_local_operation(start)
            await _refresh_continuation(
                context, workspace_id, successor.workspace.workspace_id, actor
            )
        except (
            CorrectionOriginError,
            WorkspaceError,
            ValueError,
            SecretStoreError,
            ConnectorError,
        ) as error:
            _flash(request, str(error))
            return RedirectResponse(f"/workspaces/{workspace_id}/history/3", 303)
        return RedirectResponse(
            f"/workspaces/{successor.workspace.workspace_id}/mapping", 303
        )

    return router


async def _refresh_continuation(
    context: WebContext, completed_id: str, successor_id: str, actor: Actor
):
    """Refresh access outside local owners, then rebind the saved editable rules."""

    def inputs():
        schema = context.queries.get_odoo_schema_catalog(successor_id)
        state = context.queries.get(successor_id)
        if schema is None or schema.origin is not SchemaOrigin.HISTORICAL_SEED:
            return state, False
        prior = context.queries.get(completed_id)
        owner = context.target_credential_workspace(
            completed_id, workspace_state=prior, actor=actor
        )
        credential = get_target_credential(
            context.secret_store, owner, TargetCredentialRole.READ
        )
        if credential is not None:
            store_target_credential(
                context.secret_store,
                state,
                TargetCredentialRole.READ,
                credential.secret,
                persistent=False,
            )
        return state, True

    state, refresh = await run_local_operation(inputs)
    if not refresh:
        return
    await _capture_selected_schema(context, state, actor=actor)

    def rebind():
        governance = context.queries.get_schema_governance(completed_id)
        draft = context.queries.get_mapping_working_draft(successor_id)
        if governance is None or draft is None:
            raise CorrectionOriginError(
                "The saved matching rules are incomplete. Review Odoo data before continuing."
            )
        context.schema_workspace.govern(
            successor_id, business_keys=governance.business_keys, actor=actor
        )
        context.mapping_workspace.save_working_draft(
            successor_id,
            datasets=draft.definition.datasets,
            expected_version=draft.version,
            actor=actor,
        )

    await run_local_operation(rebind)


def _saved_stage(
    request: Request,
    context: WebContext,
    workspace_id: str,
    stage: int,
):
    """Project only the selected stage under one authorized local read scope."""

    actor = request_identity(request, context.identity_resolver).actor
    access = context.workspace_access.resolve(
        workspace_id,
        actor=actor,
        capability=Capability.PROTECTED_EVIDENCE_READ,
    )
    workspace = context.migration_workspaces.get(workspace_id, actor=actor)
    if workspace.state is not MigrationWorkspaceState.CLOSED:
        raise HTTPException(409, "Choose a completed load to inspect saved stages")
    state = context.queries.get(workspace_id)
    continuation = context.corrections.get(workspace_id, actor=actor)
    revision = (
        context.queries.get_mapping_revision(workspace_id) if stage == 3 else None
    )
    selection = (
        context.queries.get_mapping_source_selection(workspace_id)
        if stage in {1, 3}
        else None
    )
    schema = (
        context.queries.get_odoo_schema_catalog(workspace_id) if stage == 2 else None
    )
    staging = context.preflight.current_staging(workspace_id) if stage == 4 else None
    quality = context.quality.current_summary(workspace_id) if stage == 4 else None
    report = context.preflight.current_report(workspace_id) if stage in {5, 6} else None
    parent = (
        context.corrections.binding_for_successor(workspace_id)
        if stage in {5, 6}
        else None
    )
    plan = (
        context.corrections.current_plan(parent.completed_workspace_id)
        if parent is not None
        else None
    )
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    fields = (*plan.fields, *plan.observations) if plan is not None else ()
    page_count = max(1, (len(fields) + 49) // 50)
    page = min(page, page_count) if plan is not None else page
    rows = (
        context.preflight.readiness_rows(
            workspace_id,
            report.run_id,
            page=page,
            page_size=50,
        )
        if report is not None
        else None
    )
    prepared_rows = (
        context.queries.get_quality_review_page(
            workspace_id,
            quality.run_id,
            status="",
            dataset="",
            page=page,
            page_size=50,
        )
        if quality is not None
        else None
    )
    source_labels = (
        {
            column.stable_key: column.source_name
            for dataset in selection.datasets
            for column in dataset.columns
        }
        if selection is not None
        else {}
    )
    mapping_rows = (
        tuple(
            {
                "dataset": dataset.dataset_id,
                "model": dataset.target_model,
                "field": field.target_field,
                "source": source_labels.get(
                    field.source_column_key or "", field.value_source.value
                ),
                "constant": field.literal_value,
                "blank": field.null_policy,
            }
            for dataset in revision.definition.datasets
            for field in dataset.fields
        )
        if revision is not None
        else ()
    )
    return _render(
        request,
        "workspace_history.html",
        workspace_state=state,
        stage=stage,
        stage_label=STAGE_LABELS[stage - 1],
        stage_labels=STAGE_LABELS,
        continuation=continuation,
        selection=selection,
        saved_schema=schema,
        saved_revision=revision,
        mapping_rows=mapping_rows,
        staging=staging,
        saved_quality=quality,
        prepared_rows=prepared_rows,
        saved_report=report,
        saved_rows=rows,
        page=page,
        request_id=str(uuid4()),
        saved_plan=plan,
        can_restore_origin=bool(parent is not None and plan is not None and continuation is None),
        plan_summary=plan.public_summary() if plan else None,
        plan_rows=fields[(page - 1) * 50 : page * 50],
        plan_page_count=page_count,
        recipe_source_url=f"/projects/{access.project_id}?recipe_workspace={workspace_id}#project-recipes-title",
    )
