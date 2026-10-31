"""Edit and inspect business expectations from Final review.

The normal review consumes published aggregates. Explicit detail reads expose
one model and at most fifty field effects through the authorized application
port. Neither action contacts Odoo. See docs/developer/workflow/05-final-review.md.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from impodo.domain.shared.access import Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.context import WebContext
from impodo.web.forms import _mapping_request_form, _secure_form
from impodo.web.matching_expectations import (
    expectation_form_fields,
    expectations_editor_view,
)
from impodo.web.presenters.common import _flash, _render
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session

from .matching_expectations import save_expectation_form


def build_business_expectations_router(context: WebContext) -> APIRouter:
    """Keep complete local callbacks off the event loop and close every owner."""
    router = APIRouter()

    def editor(
        request: Request,
        workspace_id: str,
        *,
        error="",
        submitted=None,
        status_code=200,
    ):
        actor = request_identity(request, context.identity_resolver).actor
        context.workspace_states.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=workspace_id
        )
        packet = None
        saved = context.business_intents.get(workspace_id, actor=actor)
        try:
            packet = context.business_intents.inputs(workspace_id, actor=actor)
        except ValueError as unavailable:
            error = error or str(unavailable)
        expectations = expectations_editor_view(packet, saved) if packet else None
        return _render(
            request,
            "workspace_business_expectations.html",
            workspace_id=workspace_id,
            expectations=expectations,
            editor_error=error,
            submitted=submitted,
            expectations_open=True,
            expectations_action=f"/workspaces/{workspace_id}/summary/expectations",
            can_edit=actor.has(Capability.MAPPING_EDIT),
            status_code=status_code,
        )

    @router.get("/workspaces/{workspace_id}/summary/expectations")
    async def edit(request: Request, workspace_id: str):
        require_session(request)
        return await run_page_read(editor, request, workspace_id)

    @router.post("/workspaces/{workspace_id}/summary/expectations")
    async def save(request: Request, workspace_id: str):
        require_session(request)
        form = await _mapping_request_form(request)
        _secure_form(request, form, expectation_form_fields())
        actor = request_identity(request, context.identity_resolver).actor
        try:
            _saved, assessment = await run_local_operation(
                save_expectation_form, context, workspace_id, form, actor
            )
        except (ValueError, WorkspaceError) as error:
            return await run_page_read(
                editor,
                request,
                workspace_id,
                error=str(error),
                submitted=form,
                status_code=422,
            )
        _flash(
            request,
            "Expectations saved. "
            + (
                "The proposed changes meet them."
                if assessment and assessment.ready
                else "Review the expected and proposed changes before loading."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/summary#expected-versus-found", status_code=303
        )

    def detail(request: Request, workspace_id: str):
        actor = request_identity(request, context.identity_resolver).actor
        query = request.query_params
        try:
            result = context.preflight.business_intent_evidence(
                workspace_id,
                query.get("snapshot", ""),
                actor=actor,
                expected_assessment_hash=query.get("assessment", ""),
                model=query.get("model", ""),
                page=int(query.get("page", "1")),
                run_id=query.get("run", ""),
            )
        except (ValueError, WorkspaceError) as error:
            return _render(
                request,
                "workspace_business_expectation_detail.html",
                workspace_id=workspace_id,
                evidence=None,
                evidence_error=str(error),
                status_code=409,
            )
        return _render(
            request,
            "workspace_business_expectation_detail.html",
            workspace_id=workspace_id,
            evidence=result,
            evidence_error="",
        )

    @router.get("/workspaces/{workspace_id}/summary/expectations/review")
    async def review(request: Request, workspace_id: str):
        require_session(request)
        return await run_page_read(detail, request, workspace_id)

    return router
