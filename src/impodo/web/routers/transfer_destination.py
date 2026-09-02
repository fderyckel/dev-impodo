"""Connect a separately bound destination for an Odoo-source transfer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.odoo_connection_service import OdooConnectionPurpose
from impodo.application.odoo_read_failures import OdooReadCredentialMissingError
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateError,
    transfer_destination_identity_hash,
    transfer_destination_workspace,
)

from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRemovalReason,
    TargetCredentialRole,
    audit_removed_target_credentials,
    audit_stored_target_credential,
    delete_target_credential,
    get_target_credential,
    get_target_credential_status,
    store_target_credential,
)


def _credential_persistent(form) -> bool:
    storage = _text(form, "read_api_key_storage")
    if storage == "vault":
        return True
    if storage == "session":
        return False
    raise SecretStoreError("Choose how long Impodo should keep the destination key")


def _has_frozen_source(context: WebContext, workspace_id: str) -> bool:
    try:
        return context.queries.get_source_selection(workspace_id) is not None
    except WorkspaceError:
        return False


def _render_destination(
    request: Request,
    context: WebContext,
    workspace_state,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    destination_target = (
        transfer_destination_workspace(workspace_state)
        if workspace_state.destination_configured
        else None
    )
    return _render(
        request,
        "workspace_transfer_destination.html",
        workspace_state=workspace_state,
        destination_connection=(
            context.remote_connections.get(
                destination_target,
                OdooConnectionPurpose.TARGET_READ,
            )
            if destination_target is not None
            else None
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


def build_transfer_destination_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/workspaces/{workspace_id}/transfer-destination",
        response_class=HTMLResponse,
    )
    async def transfer_destination_form(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.source_mode is not SourceMode.ODOO
            or not _has_frozen_source(context, workspace_id)
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        return _render_destination(request, context, workspace_state)

    @router.post("/workspaces/{workspace_id}/transfer-destination")
    async def transfer_destination(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "odoo_connection_mode",
                "odoo_base_url",
                "odoo_database",
                "read_api_key",
                "read_api_key_storage",
            },
        )
        current = context.queries.get(workspace_id)
        if (
            current.source_mode is not SourceMode.ODOO
            or not _has_frozen_source(context, workspace_id)
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        destination_target = None
        try:
            previous_target_hash = transfer_destination_identity_hash(current)
            workspace_state = context.workspace_states.configure_transfer_destination(
                workspace_id,
                actor=context.actor,
                expected_revision=_revision(form),
                odoo_connection_mode=_text(form, "odoo_connection_mode"),
                odoo_base_url=_text(form, "odoo_base_url"),
                odoo_database=_text(form, "odoo_database"),
            )
            destination_target_hash = transfer_destination_identity_hash(
                workspace_state
            )
            if previous_target_hash and previous_target_hash != destination_target_hash:
                receipt = delete_target_credential(
                    context.secret_store,
                    current,
                    TargetCredentialRole.DESTINATION_TRANSFER,
                    reason=TargetCredentialRemovalReason.TARGET_CHANGED,
                )
                if receipt is not None:
                    audit_removed_target_credentials(
                        context.workspace_states,
                        current,
                        (receipt,),
                        actor=context.actor,
                    )
                context.remote_connections.clear(
                    workspace_id,
                    OdooConnectionPurpose.TARGET_READ,
                )

            submitted_key = _text(form, "read_api_key")
            if submitted_key:
                credential = store_target_credential(
                    context.secret_store,
                    workspace_state,
                    TargetCredentialRole.DESTINATION_TRANSFER,
                    submitted_key,
                    persistent=_credential_persistent(form),
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    workspace_state,
                    TargetCredentialRole.DESTINATION_TRANSFER,
                    credential,
                    actor=context.actor,
                )
            else:
                credential = get_target_credential(
                    context.secret_store,
                    workspace_state,
                    TargetCredentialRole.DESTINATION_TRANSFER,
                )
            if credential is None:
                raise OdooReadCredentialMissingError(
                    "Enter the API key that will be used for this destination transfer"
                )

            destination_target = transfer_destination_workspace(workspace_state)
            result = await run_in_threadpool(
                context.odoo_connection_tests.test_read,
                destination_target,
                credential.secret,
                purpose=OdooConnectionPurpose.TARGET_READ,
            )
            checked = context.remote_connections.mark_checked(
                destination_target,
                result.fingerprint,
                result.read_identity,
                purpose=OdooConnectionPurpose.TARGET_READ,
            )
            if not checked.ready:
                raise WorkspaceStateError(
                    checked.support_code
                    or "The destination connection check did not complete"
                )
            workspace_state = context.workspace_states.verify_transfer_destination(
                workspace_id,
                actor=context.actor,
                expected_revision=workspace_state.revision,
                target_hash=result.connection.identity_hash,
                credential_binding_hash=credential.binding_hash,
                read_principal_hash=result.read_identity.principal_hash,
                odoo_version=result.fingerprint.odoo_version,
            )
        except (
            ConnectorError,
            SecretStoreError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            workspace_state = context.queries.get(workspace_id)
            if destination_target is None and workspace_state.destination_configured:
                destination_target = transfer_destination_workspace(workspace_state)
            if destination_target is not None:
                context.remote_connections.mark_error(
                    destination_target,
                    error,
                    purpose=OdooConnectionPurpose.TARGET_READ,
                )
            return _render_destination(
                request,
                context,
                workspace_state,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Destination Odoo connection verified. Nothing was changed in Odoo.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/transfer-destination",
            status_code=303,
        )

    return router
