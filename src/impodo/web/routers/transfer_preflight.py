"""Stage 8A read-only destination preflight and aggregate dry-run preview."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.application.transfer_preflight_service import TransferPreflightService
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


_BLOCKER_LABELS = {
    "DESTINATION_PERMISSION_DRIFT": "Destination permissions changed",
    "DESTINATION_CONTEXT_DRIFT": "Destination company context changed",
    "DESTINATION_MODEL_SCOPE_DRIFT": "Destination record-type scope changed",
    "DESTINATION_MATCH_KEY_DRIFT": "Matching-key scope changed",
    "DESTINATION_RECORD_CLASSIFICATION_DRIFT": "Create/update classification changed",
    "DESTINATION_RECORD_IDENTITY_DRIFT": (
        "A matching key resolves to a different destination record"
    ),
    "DESTINATION_FIELD_SCOPE_DRIFT": "Compatible field scope changed",
    "DESTINATION_RELATIONSHIP_SCOPE_DRIFT": "Relationship scope changed",
    "DESTINATION_RELATIONSHIP_RESOLUTION_DRIFT": "Relationship resolution changed",
    "SOURCE_KEY_BLANK": "A source matching key is blank",
    "SOURCE_KEY_DUPLICATE": "A source matching key is duplicated",
    "DESTINATION_KEY_DUPLICATE": "A destination matching key is duplicated",
    "DESTINATION_MATCH_LIMIT_REACHED": "The bounded destination read reached its limit",
    "DESTINATION_FIELDS_MISSING": "Destination fields are missing",
    "DESTINATION_FIELDS_INCOMPATIBLE": "Destination fields are incompatible",
    "SOURCE_RELATION_EVIDENCE_MISSING": "Protected relationship evidence is unavailable",
    "SOURCE_REQUIRED_RELATION_BLANK": "A required source relationship is blank",
    "SOURCE_RELATED_RECORD_MISSING": "A related source record is missing",
    "DESTINATION_RELATED_KEY_DUPLICATE": "A related destination key is duplicated",
}


def _current_evidence(context: WebContext, workspace_id: str):
    selection = context.queries.get_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if selection is None or schema is None:
        raise WorkspaceError("Freeze the Odoo source before destination preflight")
    return selection, schema


def _labels(codes) -> tuple[str, ...]:
    return tuple(_BLOCKER_LABELS.get(code, code.replace("_", " ").title()) for code in codes)


def _dataset_rows(package, report):
    if package is None or report is None:
        return ()
    approved = {item.dataset_id: item for item in package.datasets}
    return tuple(
        {
            "result": item,
            "approved": approved[item.dataset_id],
            "blockers": _labels(item.blocker_codes),
        }
        for item in report.datasets
    )


def _relationship_rows(package, report):
    if package is None or report is None:
        return ()
    approved = {
        (item.owner_dataset_id, item.field_name): item
        for item in package.relationships
    }
    return tuple(
        {
            "result": item,
            "approved": approved[(item.owner_dataset_id, item.field_name)],
            "blockers": _labels(item.blocker_codes),
        }
        for item in report.relationships
    )


def _render_preflight(
    request: Request,
    context: WebContext,
    workspace_state,
    selection,
    schema,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    approved = workspace_state.transfer_review_approved(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    current = workspace_state.transfer_preflight_current(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    report = workspace_state.transfer_preflight_report
    package = workspace_state.transfer_review_package
    return _render(
        request,
        "workspace_transfer_preflight.html",
        workspace_state=workspace_state,
        transfer_review_package=package,
        transfer_review_approved=approved,
        transfer_preflight_report=report,
        transfer_preflight_current=current,
        transfer_preflight_ready=bool(current and report and report.ready),
        transfer_preflight_stale=report is not None and not current,
        transfer_preflight_blockers=_labels(report.blocker_codes) if report else (),
        transfer_preflight_dataset_rows=_dataset_rows(
            package if current else None,
            report if current else None,
        ),
        transfer_preflight_relationship_rows=_relationship_rows(
            package if current else None,
            report if current else None,
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


def build_transfer_preflight_router(context: WebContext) -> APIRouter:
    router = APIRouter()
    matching = DestinationMatchingService(context.categorical_coverage)
    preflight = TransferPreflightService()

    @router.get(
        "/workspaces/{workspace_id}/transfer-preflight",
        response_class=HTMLResponse,
    )
    async def transfer_preflight_form(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.source_mode is not SourceMode.ODOO:
            return RedirectResponse(f"/workspaces/{workspace_id}/sources", status_code=303)
        try:
            selection, schema = _current_evidence(context, workspace_id)
        except WorkspaceError:
            return RedirectResponse(f"/workspaces/{workspace_id}/sources", status_code=303)
        if not workspace_state.transfer_review_approved(
            source_selection_hash=selection.content_hash,
            source_schema_hash=schema.content_hash,
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-review",
                status_code=303,
            )
        return _render_preflight(
            request,
            context,
            workspace_state,
            selection,
            schema,
        )

    @router.post("/workspaces/{workspace_id}/transfer-preflight")
    async def run_transfer_preflight(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        workspace_state = context.queries.get(workspace_id)
        try:
            selection, schema = _current_evidence(context, workspace_id)
        except WorkspaceError as error:
            _flash(request, str(error))
            return RedirectResponse(f"/workspaces/{workspace_id}/sources", status_code=303)
        try:
            expected_revision = _revision(form)
            if expected_revision != workspace_state.revision:
                raise WorkspaceStateError(
                    "The workspace changed in another request; reload before continuing"
                )
            package = workspace_state.transfer_review_package
            approval = workspace_state.transfer_review_approval
            approved_match = workspace_state.destination_match_plan
            if package is None or approval is None or approved_match is None:
                raise WorkspaceStateError("Approve the current transfer package first")
            if not workspace_state.transfer_review_approved(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            ):
                raise WorkspaceStateError("Approve the current transfer package first")
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
            choices = tuple(
                DestinationMatchKeyChoice(
                    dataset_id=item.dataset_id,
                    source_column_key=item.source_column_key,
                )
                for item in approved_match.model_matches
            )
            fresh_match = await run_in_threadpool(
                matching.check,
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
            report = preflight.build(
                workspace_state,
                package,
                approval,
                approved_match,
                fresh_match,
                recorded_by=context.actor.identity,
            )
            workspace_state = context.workspace_states.save_transfer_preflight_report(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                report=report,
            )
        except (
            ConnectorError,
            SecretStoreError,
            WorkspaceError,
            WorkspaceStateError,
            PermissionError,
            ValueError,
        ) as error:
            return _render_preflight(
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
                "Read-only destination preflight passed. No Odoo data was changed."
                if workspace_state.transfer_preflight_report.ready
                else "Destination preflight found drift. No Odoo data was changed."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-preflight#preflight-results",
            status_code=303,
        )

    return router
