"""Stage-G review routes for group decisions and eligible-dataset approval."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...domain.errors import ReadinessError
from ...workspace_state import WorkspaceStateError, SourceMode
from ...workspace_errors import WorkspaceError
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash
from ..presenters.summary import _render_normalization
from ..security import require_session


_REVIEW_STATUSES = frozenset(
    {"automatic", "pending", "reviewed", "set_aside"}
)


def _review_return_url(request: Request, workspace_id: str) -> str:
    """Keep the current review slice and land beside the decision table."""

    status = request.query_params.get("status", "").strip()
    if status not in _REVIEW_STATUSES:
        status = ""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    query = {}
    if status:
        query["status"] = status
    if page > 1:
        query["page"] = page
    suffix = f"?{urlencode(query)}" if query else ""
    return f"/workspaces/{workspace_id}/normalization{suffix}#review-groups"


def build_normalization_router(context: WebContext) -> APIRouter:
    """Build review, group-decision, and final-freeze HTTP actions."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/normalization", response_class=HTMLResponse)
    async def review_prepared_data(request: Request, workspace_id: str):
        require_session(request)
        return _render_normalization(request, context, workspace_id)

    @router.post(
        "/workspaces/{workspace_id}/normalization/groups/{group_id}/accept"
    )
    async def accept_prepared_change(
        request: Request,
        workspace_id: str,
        group_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.normalization.decide_group,
                workspace_id,
                str(form["run_id"]),
                group_id,
                approve=True,
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared change accepted.")
        return RedirectResponse(
            _review_return_url(request, workspace_id),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/normalization/reopen")
    async def reopen_prepared_review(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.normalization.reopen_review,
                workspace_id,
                str(form["run_id"]),
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
                reason="Reopened by the data manager after a sent-back change.",
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared review reopened. Review or approve the changes.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/normalization?status=pending#review-groups",
            status_code=303,
        )

    @router.post(
        "/workspaces/{workspace_id}/normalization/groups/{group_id}/reject"
    )
    async def reject_prepared_change(
        request: Request,
        workspace_id: str,
        group_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version", "reason"},
        )
        try:
            reason = str(form["reason"]).strip()
            if not reason:
                raise WorkspaceStateError("Explain what needs fixing before continuing")
            if len(reason) > 1000:
                raise WorkspaceStateError("The explanation is too long")
            await run_in_threadpool(
                context.normalization.decide_group,
                workspace_id,
                str(form["run_id"]),
                group_id,
                approve=False,
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
                reason=reason,
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared change sent back for correction.")
        return RedirectResponse(
            _review_return_url(request, workspace_id),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/normalization/approve")
    async def approve_prepared_data(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.normalization.approve,
                workspace_id,
                str(form["run_id"]),
                expected_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return _render_normalization(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.source_mode is SourceMode.ODOO:
            _flash(
                request,
                "Prepared Odoo records approved. Compare them with Odoo next.",
            )
            return RedirectResponse(
                f"/workspaces/{workspace_id}/summary",
                status_code=303,
            )
        _flash(request, "Prepared data approved. You can now compare it with Odoo.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/summary",
            status_code=303,
        )

    return router

