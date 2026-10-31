"""Expose one protected, bounded relationship-choice page from saved evidence.

Local projection and rendering share one synchronous read operation. Server
composition must provide an equivalent authorized worker-receipt query port.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from impodo.domain.relationship_review import RelationshipChoiceFilter
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_page_read
from impodo.web.context import WebContext
from impodo.web.presenters.common import _flash, _render
from impodo.web.presenters.relationship_choice_review import (
    relationship_choice_page_view,
)
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session


def build_relationship_choice_review_router(context: WebContext) -> APIRouter:
    """Keep protected detail reads behind the existing application authority."""
    router = APIRouter()

    def render(request: Request, workspace_id: str):
        actor = request_identity(request, context.identity_resolver).actor
        context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ
        )
        query = request.query_params

        def cursor(name):
            value = query.get(name, "")
            if not value:
                return None
            if not value.isascii() or not value.isdecimal() or len(value) > 19:
                raise WorkspaceError("Choose a valid relationship-choice page")
            return int(value)

        filters = RelationshipChoiceFilter(
            query.get("dataset", ""), query.get("field", ""), query.get("cause", "")
        )
        selection = context.queries.get_mapping_source_selection(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        governance = context.queries.get_schema_governance(workspace_id)
        draft = context.queries.get_mapping_working_draft(workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Choose and confirm source and Odoo data before reviewing relationships"
            )
        page = context.matching_order.relationship_choice_review_page(
            workspace_id,
            selection,
            schema,
            governance,
            draft,
            filters,
            page_size=100,
            actor=actor,
            after=cursor("after"),
            before=cursor("before"),
            expected_check_id=query.get("check", ""),
        )
        response = _render(
            request,
            "mapping/relationship_choice_review.html",
            workspace_id=workspace_id,
            workspace_state=context.queries.get(workspace_id),
            page=page,
            filters=filters,
            **relationship_choice_page_view(
                workspace_id, selection, schema, draft, filters, page
            ),
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/workspaces/{workspace_id}/mapping/relationship-choices")
    async def review(request: Request, workspace_id: str):
        require_session(request)
        try:
            return await run_page_read(render, request, workspace_id)
        except (ValueError, WorkspaceError) as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/mapping#relationship-health-title",
                status_code=303,
            )

    return router
