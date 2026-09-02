"""Stage 6 deterministic transfer-order planning for Odoo-to-Odoo."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from impodo.application.transfer_order_service import TransferOrderService
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStateError

from ..context import WebContext
from ..forms import _revision, _secure_form
from ..presenters.common import _flash, _render
from ..security import require_session


def _current_evidence(context: WebContext, workspace_id: str):
    selection = context.queries.get_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if selection is None or schema is None:
        raise WorkspaceError("Freeze the Odoo source before planning transfer order")
    return selection, schema


def _view_rows(plan):
    if plan is None:
        return (), (), ()
    by_dataset = {item.dataset_id: item for item in plan.datasets}
    waves = tuple(
        {
            "wave": wave,
            "datasets": tuple(by_dataset[item] for item in wave.dataset_ids),
        }
        for wave in plan.waves
    )
    dependencies = tuple(
        {
            "dependency": item,
            "owner": by_dataset[item.owner_dataset_id],
            "required": by_dataset[item.dependency_dataset_id],
        }
        for item in plan.dependencies
    )
    blockers = tuple(
        {
            "blocker": item,
            "dataset": by_dataset[item.dataset_id],
            "dependency": by_dataset.get(item.dependency_dataset_id),
        }
        for item in plan.blockers
    )
    return waves, dependencies, blockers


def _render_order(
    request: Request,
    workspace_state,
    selection,
    schema,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    current = workspace_state.transfer_order_current(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    plan = workspace_state.transfer_order_plan
    ready = bool(current and plan is not None and plan.ready)
    waves, dependencies, blockers = _view_rows(plan)
    return _render(
        request,
        "workspace_transfer_order.html",
        workspace_state=workspace_state,
        source_selection=selection,
        source_schema=schema,
        transfer_order_plan=plan,
        transfer_order_current=current,
        transfer_order_ready=ready,
        transfer_order_stale=plan is not None and not current,
        transfer_order_waves=waves,
        transfer_order_dependencies=dependencies,
        transfer_order_blockers=blockers,
        disable_default_read_credential_prompt=True,
        error=error,
        status_code=status_code,
    )


def build_transfer_order_router(context: WebContext) -> APIRouter:
    router = APIRouter()
    service = TransferOrderService()

    @router.get(
        "/workspaces/{workspace_id}/transfer-order",
        response_class=HTMLResponse,
    )
    async def transfer_order_form(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.source_mode is not SourceMode.ODOO:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        try:
            selection, schema = _current_evidence(context, workspace_id)
        except WorkspaceError:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        if not workspace_state.destination_match_ready(
            source_selection_hash=selection.content_hash,
            source_schema_hash=schema.content_hash,
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/destination-matching",
                status_code=303,
            )
        return _render_order(
            request,
            workspace_state,
            selection,
            schema,
        )

    @router.post("/workspaces/{workspace_id}/transfer-order")
    async def build_transfer_order(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        workspace_state = context.queries.get(workspace_id)
        try:
            selection, schema = _current_evidence(context, workspace_id)
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
            if not workspace_state.destination_match_ready(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            ):
                raise WorkspaceError("Complete current destination matching first")
            match_plan = workspace_state.destination_match_plan
            assert match_plan is not None
            plan = service.build(
                workspace_state,
                match_plan,
                recorded_by=context.actor.identity.display_name,
            )
            workspace_state = context.workspace_states.save_transfer_order_plan(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                plan=plan,
            )
        except (WorkspaceError, WorkspaceStateError) as error:
            return _render_order(
                request,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                "Transfer order is ready. Nothing was changed in Odoo."
                if workspace_state.transfer_order_plan.ready
                else "Transfer order was built. Review the dependency blockers."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-order#transfer-order-results",
            status_code=303,
        )

    return router
