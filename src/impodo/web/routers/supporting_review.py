"""Save and explicitly check supporting decisions across bounded operations."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.compatibility import OdooOperation
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import ReferenceReadPurpose
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
    SupportingChoiceValidationError,
)
from impodo.web.composition.page_reads import (
    run_local_operation,
    run_local_operation_sync,
    run_page_read,
)
from impodo.web.composition.target_readers import _capture_supporting_values
from impodo.web.context import WebContext
from impodo.web.forms import _secure_form, _text
from impodo.web.presenters.common import _flash
from impodo.web.presenters.mapping_view import _active_mapping_state
from impodo.web.presenters.schema import _render_schema
from impodo.web.presenters.supporting_review import mapping_supporting_review_view
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session


def build_supporting_review_router(context: WebContext) -> APIRouter:
    """Install commands without adding any implicit Odoo read to page rendering."""
    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/mapping/supporting-review")
    async def matching_review(request: Request, workspace_id: str):
        """Reload only recovery controls; leave the browser's field edits intact."""
        require_session(request)
        actor = request_identity(request, context.identity_resolver).actor

        def render():
            packet = context.schema_workspace.supporting_review_inputs(workspace_id, actor=actor)
            mapping_selection = context.queries.get_mapping_source_selection(workspace_id)
            _current, definition = _active_mapping_state(
                mapping_selection, packet.schema, packet.governance,
                context.queries.get_mapping_revision(workspace_id),
                context.queries.get_mapping_working_draft(workspace_id),
            )
            view = mapping_supporting_review_view(
                request, context, packet.workspace, packet.selection, packet.schema,
                packet.governance, actor=actor, preparation=packet.preparation,
                mapping_selection=mapping_selection,
                mapping_datasets=definition.datasets if definition else None,
            )
            return request.app.state.templates.TemplateResponse(
                request=request,
                name="mapping/_supporting_review.html",
                context={
                    "workspace_id": workspace_id,
                    "related_data_review": view,
                    "inline_supporting_review": True,
                    "related_review_requested": True,
                    "csrf_token": request.session.get("csrf_token", ""),
                },
                headers={"Cache-Control": "no-store"},
            )

        return await run_page_read(render)

    @router.post("/workspaces/{workspace_id}/schema/supporting/save")
    async def save_supporting_choices(request: Request, workspace_id: str):
        json_request = "application/json" in request.headers.get("accept", "")
        form = await request.form()
        try:
            count = int(_text(form, "choice_count"))
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="Supporting choice count is invalid"
            ) from error
        if not 0 <= count <= 1_000:
            raise HTTPException(
                status_code=400, detail="Supporting choice count exceeds its limit"
            )
        allowed = {
            "csrf_token",
            "binding_hash",
            "expected_version",
            "operation_id",
            "choice_count",
        }
        allowed.update(
            f"{prefix}_{index}"
            for index in range(count)
            for prefix in ("edge", "action", "dataset")
        )
        _secure_form(request, form, allowed)
        actor = request_identity(request, context.identity_resolver).actor
        submitted = tuple(
            (
                _text(form, f"edge_{index}"),
                _text(form, f"action_{index}"),
                _text(form, f"dataset_{index}"),
            )
            for index in range(count)
        )
        try:
            choices = []
            for edge, action, dataset in submitted:
                if not action:
                    continue
                model, field = edge.split("|", 1)
                try:
                    choice = SupportingChoice(
                        model,
                        field,
                        SupportingChoiceAction(action),
                        dataset if action == "INCOMING" else "",
                    )
                except ValueError as error:
                    message = (
                        "Choose an accepted incoming table."
                        if action == "INCOMING" and not dataset
                        else "Review the source and table for this relationship."
                    )
                    raise SupportingChoiceValidationError(
                        str(error), (model, field), field_message=message
                    ) from error
                choices.append(choice)

            def save():
                context.schema_workspace.authorization.require(
                    actor, Capability.SCHEMA_GOVERN, workspace_id=workspace_id
                )
                packet = context.schema_workspace.supporting_review_inputs(
                    workspace_id, actor=actor
                )
                return context.schema_workspace.supporting.save(
                    packet,
                    expected_binding_hash=_text(form, "binding_hash"),
                    expected_version=int(_text(form, "expected_version")),
                    operation_id=_text(form, "operation_id"),
                    choices=tuple(choices),
                    actor=actor,
                )

            review = await run_local_operation(save)
        except (ValueError, WorkspaceError) as error:
            if json_request:
                return JSONResponse({"detail": str(error)}, status_code=422)
            return await run_page_read(
                _render_schema,
                request,
                context,
                workspace_id,
                supporting_save_error=error,
                supporting_drafts={edge: (action, dataset) for edge, action, dataset in submitted},
                status_code=422,
            )
        if json_request:
            return JSONResponse({"version": review.version, "binding_hash": review.binding_hash})
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#supporting-data-review", status_code=303
        )

    @router.post("/workspaces/{workspace_id}/schema/supporting/check")
    async def check_supporting_values(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "binding_hash", "expected_version"})
        actor = request_identity(request, context.identity_resolver).actor
        try:

            def prepare():
                context.schema_workspace.authorization.require(
                    actor, Capability.SCHEMA_DISCOVER, workspace_id=workspace_id
                )
                context.schema_workspace.authorization.require(
                    actor, Capability.MAPPING_EDIT, workspace_id=workspace_id
                )
                packet = context.schema_workspace.supporting_review_inputs(
                    workspace_id, actor=actor
                )
                review = context.schema_workspace.supporting.require_current(
                    packet, actor=actor
                )
                if (
                    review is None
                    or review.version != int(_text(form, "expected_version"))
                    or packet.binding_hash != _text(form, "binding_hash")
                ):
                    raise WorkspaceError(
                        "Supporting choices changed; open the current review again"
                    )
                requirements = context.schema_workspace.supporting.requirements(
                    packet, review, actor=actor
                )
                return packet, review, requirements

            packet, review, requirements = await run_local_operation(prepare)

            def publish_operation(publish):
                def verify_and_publish():
                    current, saved, _requirements = prepare()
                    if (
                        current.binding_hash != packet.binding_hash
                        or saved.content_hash != review.content_hash
                    ):
                        raise WorkspaceError(
                            "Supporting inputs changed during the read; check again"
                        )
                    return publish()

                return run_local_operation_sync(verify_and_publish)

            snapshots = await run_in_threadpool(
                _capture_supporting_values,
                context,
                packet.workspace,
                packet.schema,
                requirements,
                purpose=ReferenceReadPurpose.MATCH_CHOICES,
                operation=OdooOperation.COMPARE,
                workflow_label="reviewed supporting data",
                actor=actor,
                publish_operation=publish_operation,
                require_current_access=True,
            )
        except (ValueError, WorkspaceError, ConnectorError, SecretStoreError) as error:
            return await run_page_read(
                _render_schema,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Checked {len(snapshots)} supporting value sets. Match data still checks the actual mapped choices.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#supporting-data-review", status_code=303
        )

    return router
