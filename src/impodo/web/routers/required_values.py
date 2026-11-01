"""Serve required-value coverage from bulk saved metadata in one read scope.

Server composition must supply verified worker projections through the existing
query ports. This route starts no capture, source evaluation, or validation.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from impodo.application.workspace.mapping.required_values import ValueCoverageState
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_page_read
from impodo.web.context import WebContext
from impodo.web.presenters.common import _flash, _render
from impodo.web.presenters.required_values import required_values_view
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session


def build_required_values_router(context: WebContext) -> APIRouter:
    """Expose one advisory entry point, preserving existing command ownership."""
    router = APIRouter()

    def render(request, workspace_id):
        actor = request_identity(request, context.identity_resolver).actor
        context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.MAPPING_EDIT
        )
        projection = context.required_values.review(workspace_id, actor=actor)
        dataset, state = (
            request.query_params.get("dataset", ""),
            request.query_params.get("state", ""),
        )
        if dataset and dataset not in {item.dataset_id for item in projection.tables}:
            raise WorkspaceError("Choose a current table for required-value review")
        if state:
            ValueCoverageState(state)
        page = request.query_params.get("page", "1")
        if not page.isascii() or not page.isdecimal() or len(page) > 7 or int(page) < 1:
            raise WorkspaceError("Choose a bounded required-value page")
        response = _render(
            request,
            "mapping/required_values.html",
            workspace_id=workspace_id,
            workspace_state=context.queries.get(workspace_id),
            **required_values_view(
                workspace_id,
                projection,
                dataset=dataset,
                state=state,
                page=int(page),
            ),
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/workspaces/{workspace_id}/mapping/required-values")
    async def required_values(request: Request, workspace_id: str):
        require_session(request)
        try:
            return await run_page_read(render, request, workspace_id)
        except (ValueError, WorkspaceError) as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/mapping", status_code=303
            )

    return router
