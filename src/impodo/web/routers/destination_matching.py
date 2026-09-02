"""Stage 5 destination matching for frozen Odoo-source transfers."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
    destination_match_key_candidates,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateError,
    transfer_destination_workspace,
)

from ..context import WebContext
from ..forms import _revision, _secure_form
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    get_target_credential_status,
)


def _matching_evidence(context: WebContext, workspace_id: str):
    selection = context.queries.get_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if selection is None or schema is None:
        raise WorkspaceError("Freeze the Odoo source before matching the destination")
    return selection, schema


def _choice_value(dataset_id: str, source_column_key: str) -> str:
    return f"{dataset_id}::{source_column_key}"


def _parse_choices(form) -> tuple[DestinationMatchKeyChoice, ...]:
    choices: list[DestinationMatchKeyChoice] = []
    for raw in form.getlist("match_key"):
        dataset_id, separator, source_column_key = str(raw).partition("::")
        if not separator or not dataset_id or not source_column_key:
            raise WorkspaceStateError("Choose one matching field for each source table")
        choices.append(
            DestinationMatchKeyChoice(
                dataset_id=dataset_id,
                source_column_key=source_column_key,
            )
        )
    return tuple(choices)


def _matching_rows(workspace_state, selection, schema):
    candidates = destination_match_key_candidates(selection, schema)
    plan = workspace_state.destination_match_plan
    selected_by_dataset = {
        item.dataset_id: item.source_column_key
        for item in plan.model_matches
    } if plan is not None else {}
    result_by_dataset = {
        item.dataset_id: item for item in plan.model_matches
    } if plan is not None else {}
    rows = []
    for dataset in selection.datasets:
        model = (
            dataset.source.model
            if isinstance(dataset.source, OdooSourceBinding)
            else ""
        )
        available = tuple(
            {
                "stable_key": stable_key,
                "field_name": field_name,
                "label": label,
                "value": _choice_value(dataset.dataset_id, stable_key),
            }
            for stable_key, field_name, label in candidates.get(dataset.dataset_id, ())
        )
        selected_key = selected_by_dataset.get(dataset.dataset_id)
        if selected_key not in {item["stable_key"] for item in available}:
            selected_key = available[0]["stable_key"] if available else ""
        rows.append(
            {
                "dataset": dataset,
                "model": model,
                "candidates": available,
                "selected_key": selected_key,
                "result": result_by_dataset.get(dataset.dataset_id),
            }
        )
    return tuple(rows)


def _render_matching(
    request: Request,
    context: WebContext,
    workspace_state,
    selection,
    schema,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    current = workspace_state.destination_match_current(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    ready = bool(
        current
        and workspace_state.destination_match_plan is not None
        and workspace_state.destination_match_plan.ready
    )
    rows = _matching_rows(workspace_state, selection, schema)
    return _render(
        request,
        "workspace_destination_matching.html",
        workspace_state=workspace_state,
        source_selection=selection,
        source_schema=schema,
        matching_rows=rows,
        matching_can_check=bool(rows) and all(row["candidates"] for row in rows),
        match_plan=workspace_state.destination_match_plan,
        match_plan_current=current,
        match_plan_ready=ready,
        match_plan_stale=(
            workspace_state.destination_match_plan is not None and not current
        ),
        destination_credential_status=get_target_credential_status(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.DESTINATION_TRANSFER,
        ),
        disable_default_read_credential_prompt=True,
        error=error,
        status_code=status_code,
    )


def build_destination_matching_router(context: WebContext) -> APIRouter:
    router = APIRouter()
    service = DestinationMatchingService(context.categorical_coverage)

    @router.get(
        "/workspaces/{workspace_id}/destination-matching",
        response_class=HTMLResponse,
    )
    async def destination_matching_form(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.source_mode is not SourceMode.ODOO
            or not workspace_state.destination_verified
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-destination",
                status_code=303,
            )
        try:
            selection, schema = _matching_evidence(context, workspace_id)
        except WorkspaceError:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        return _render_matching(
            request,
            context,
            workspace_state,
            selection,
            schema,
        )

    @router.post("/workspaces/{workspace_id}/destination-matching")
    async def check_destination_matching(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "match_key"},
        )
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.source_mode is not SourceMode.ODOO
            or not workspace_state.destination_verified
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-destination",
                status_code=303,
            )
        try:
            selection, schema = _matching_evidence(context, workspace_id)
        except WorkspaceError as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        try:
            expected_revision = _revision(form)
            if expected_revision != workspace_state.revision:
                raise WorkspaceStateError(
                    "The workspace changed in another request; reload before continuing"
                )
            choices = _parse_choices(form)
            credential = get_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.DESTINATION_TRANSFER,
            )
            if credential is None:
                raise SecretStoreError(
                    "Return to the destination connection and enter its transfer key"
                )
            models = tuple(
                sorted(
                    item.source.model
                    for item in selection.datasets
                    if isinstance(item.source, OdooSourceBinding)
                )
            )
            destination = replace(
                transfer_destination_workspace(workspace_state),
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
            plan = await run_in_threadpool(
                service.check,
                workspace_state,
                selection,
                schema,
                choices,
                api_key=credential.secret,
                credential_binding_hash=credential.binding_hash,
                read_identity=identity,
                reader=context.destination_match_reader,
                recorded_by=context.actor.identity.display_name,
                source_origins=source_origins,
            )
            workspace_state = context.workspace_states.save_destination_match_plan(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                plan=plan,
            )
        except (
            ConnectorError,
            SecretStoreError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            return _render_matching(
                request,
                context,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                "Destination matching is ready. Nothing was changed in Odoo."
                if workspace_state.destination_match_plan.ready
                else "Destination matching was checked. Review the blockers below."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/destination-matching#matching-results",
            status_code=303,
        )

    return router
