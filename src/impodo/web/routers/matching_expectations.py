"""Save the Stage 3 business expectations through the existing authorized service.

See docs/developer/workflow/03-match-data.md.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from impodo.domain.shared.access import Capability
from impodo.domain.workspace.business_intent import BusinessIntentError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.context import WebContext
from impodo.web.forms import _mapping_request_form, _secure_form, _text
from impodo.web.matching_expectations import expectation_form_fields, parse_expectations
from impodo.web.presenters.common import _flash
from impodo.web.presenters.mapping_view import _render_mapping
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session


def save_expectation_form(context, workspace_id, form, actor):
    """Save and reassess through the same authorized command for both editors."""
    context.workspace_states.authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
    packet = context.business_intents.inputs(workspace_id, actor=actor)
    saved = context.business_intents.save(workspace_id, intent=parse_expectations(form, packet),
        expected_version=int(_text(form, "expected_version")), expected_binding_hash=_text(form, "binding_hash"),
        operation_id=_text(form, "operation_id"), actor=actor)
    try:
        assessment = context.preflight.reassess_business_intent(workspace_id, actor=actor)
    except WorkspaceError:
        assessment = None
    return saved, assessment


def build_matching_expectations_router(context: WebContext) -> APIRouter:
    """Keep metadata reads and publication within one synchronous local command."""
    router = APIRouter()

    @router.post("/workspaces/{workspace_id}/mapping/expectations")
    async def save(request: Request, workspace_id: str):
        require_session(request)
        form = await _mapping_request_form(request)
        _secure_form(request, form, expectation_form_fields())
        actor = request_identity(request, context.identity_resolver).actor

        try:
            _saved, assessment = await run_local_operation(save_expectation_form, context, workspace_id, form, actor)
        except (BusinessIntentError, ValueError, WorkspaceError) as error:
            return await run_page_read(
                _render_mapping,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Business expectations saved. "
            + (
                "The current comparison meets them."
                if assessment and assessment.ready
                else "Review a fresh comparison before loading."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/mapping#business-expectations", status_code=303
        )

    return router
