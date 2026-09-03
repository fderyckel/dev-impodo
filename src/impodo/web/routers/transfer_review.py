"""Stage 7 Odoo transfer review and exact export-plan approval."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStateError

from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session


def _current_evidence(context: WebContext, workspace_id: str):
    selection = context.queries.get_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if selection is None or schema is None:
        raise WorkspaceError("Freeze the Odoo source before reviewing the transfer")
    return selection, schema


def _dataset_rows(package):
    if package is None:
        return ()
    relations: dict[str, list] = {item.dataset_id: [] for item in package.datasets}
    for item in package.relationships:
        relations[item.owner_dataset_id].append(item)
    return tuple(
        {
            "dataset": item,
            "relationships": tuple(relations[item.dataset_id]),
        }
        for item in package.datasets
    )


def _render_review(
    request: Request,
    workspace_state,
    selection,
    schema,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    current = workspace_state.transfer_review_current(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    approved = workspace_state.transfer_review_approved(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    package = workspace_state.transfer_review_package
    return _render(
        request,
        "workspace_transfer_review.html",
        workspace_state=workspace_state,
        source_selection=selection,
        source_schema=schema,
        transfer_review_package=package,
        transfer_review_current=current,
        transfer_review_approved=approved,
        transfer_review_stale=package is not None and not current,
        transfer_review_approval=(
            workspace_state.transfer_review_approval if approved else None
        ),
        transfer_review_dataset_rows=_dataset_rows(package if current else None),
        disable_default_read_credential_prompt=True,
        error=error,
        status_code=status_code,
    )


def build_transfer_review_router(context: WebContext) -> APIRouter:
    router = APIRouter()
    service = TransferReviewService()

    @router.get(
        "/workspaces/{workspace_id}/transfer-review",
        response_class=HTMLResponse,
    )
    async def transfer_review_form(request: Request, workspace_id: str):
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
        if not workspace_state.transfer_order_ready(
            source_selection_hash=selection.content_hash,
            source_schema_hash=schema.content_hash,
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-order",
                status_code=303,
            )
        return _render_review(
            request,
            workspace_state,
            selection,
            schema,
        )

    @router.post("/workspaces/{workspace_id}/transfer-review/build")
    async def build_transfer_review(request: Request, workspace_id: str):
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
            match_plan = workspace_state.destination_match_plan
            order_plan = workspace_state.transfer_order_plan
            if match_plan is None or order_plan is None:
                raise WorkspaceError("Complete the current transfer order first")
            access = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PROJECT_EDIT,
            )
            package = service.build(
                workspace_state,
                match_plan,
                order_plan,
                run_id=access.migration_run_id,
                data_version_id=access.data_version_id,
                built_by=context.actor.identity,
            )
            workspace_state = context.workspace_states.save_transfer_review_package(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                package=package,
            )
        except (WorkspaceError, WorkspaceStateError, PermissionError, ValueError) as error:
            return _render_review(
                request,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Transfer review package is ready. Nothing was sent to Odoo.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-review#review-package",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/transfer-review/approve")
    async def approve_transfer_review(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "confirmation", "reason"},
        )
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
            if _text(form, "confirmation") != "approve":
                raise WorkspaceStateError(
                    "Confirm the exact transfer scope before approving it"
                )
            reason = _text(form, "reason")
            if len(reason) > 2_000:
                raise WorkspaceStateError("The approval note is too long")
            package = workspace_state.transfer_review_package
            if package is None or not workspace_state.transfer_review_current(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            ):
                raise WorkspaceStateError("Build the current review package first")
            approval = TransferReviewApproval.approve(
                package,
                approval_id=str(uuid4()),
                actor=context.actor,
                approved_at=datetime.now(UTC),
                reason=reason,
            )
            workspace_state = context.workspace_states.approve_transfer_review(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                approval=approval,
            )
        except (WorkspaceError, WorkspaceStateError, PermissionError, ValueError) as error:
            return _render_review(
                request,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Exact transfer package approved. No destination write has started.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-review#review-package",
            status_code=303,
        )

    return router
