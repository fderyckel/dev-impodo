"""Target browser routes."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...access import Capability
from ...application.odoo_connection_service import OdooConnectionPurpose
from ...application.odoo_read_failures import OdooReadCredentialMissingError
from ...connectors import ConnectorError
from ...local_stack import LocalStackError, LocalStackStatus, ReadinessLevel
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ...workspace_state import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceSetupStep,
    WorkspaceStateError,
    WorkspaceStatus,
    workspace_setup_requirements_for_step,
)
from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash
from ..presenters.setup import blocking_setup_url
from ..presenters.summary import (
    _render_summary,
    _render_target,
    _require_local_stack_access,
    _require_local_stack_start,
    _require_local_stack_stop,
)
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRemovalReason,
    TargetCredentialRole,
    audit_removed_target_credentials,
    audit_stored_target_credential,
    delete_target_credential,
    delete_target_credentials,
    get_target_credential,
    store_target_credential,
    target_read_credential_id,
    target_write_credential_id,
)
from ..target_readers import _selected_local_profile

_LOCAL_STACK_RETURN_TARGET = "target"
_LOCAL_STACK_RETURN_SUMMARY_COMPARE = "summary_compare"
_LOCAL_STACK_RETURN_VALUES = {
    _LOCAL_STACK_RETURN_TARGET,
    _LOCAL_STACK_RETURN_SUMMARY_COMPARE,
}


def _connection_purpose(workspace_state) -> OdooConnectionPurpose:
    """Return the read purpose represented by the shared connection page."""

    return (
        OdooConnectionPurpose.SOURCE_READ
        if workspace_state.source_mode is SourceMode.ODOO
        else OdooConnectionPurpose.TARGET_READ
    )


def _local_stack_return_to(form) -> str:
    value = _text(form, "return_to") or _LOCAL_STACK_RETURN_TARGET
    if value not in _LOCAL_STACK_RETURN_VALUES:
        raise LocalStackError("The requested local Odoo return step is unavailable.")
    return value


def _local_stack_return_location(workspace_id: str, return_to: str) -> str:
    if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
        return (
            f"/workspaces/{workspace_id}/summary?local_stack=1"
            "#compare-with-odoo"
        )
    return f"/workspaces/{workspace_id}/target?local_stack=1"


def _target_read_key_persistence(form) -> bool:
    """Prefer the explicit retention choice while accepting older forms."""

    storage = _text(form, "read_api_key_storage")
    if storage == "vault":
        return True
    if storage == "session":
        return False
    if storage:
        raise SecretStoreError("Choose a valid read-only key storage option.")
    return "remember_read_api_key" in form or "remember_api_key" in form


def _quick_credential_return_to(form, workspace_id: str) -> str:
    """Accept one same-origin path without creating an open redirect."""

    value = _text(form, "return_to")
    if not value:
        return f"/workspaces/{workspace_id}/target"
    parsed = urlsplit(value)
    decoded = unquote(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or unquote(parsed.path).startswith("//")
        or "\\" in decoded
        or any(ord(character) < 32 for character in decoded)
        or len(value) > 2048
    ):
        raise SecretStoreError("The requested return page is unavailable")
    return value


def _accepts_json(request: Request) -> bool:
    """Return JSON to the inline browser dialog without changing form encoding."""

    return "application/json" in request.headers.get("accept", "").lower()


def _render_local_stack_error(
    request: Request,
    context: WebContext,
    workspace_state,
    error: Exception,
    *,
    return_to: str,
):
    if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
        context.local_stack.mark_connection_error(
            workspace_state.workspace_id,
            detail=str(error),
        )
        return _render_summary(
            request,
            context,
            workspace_state.workspace_id,
            local_stack_error=(
                "Local Odoo is not ready yet. Review the checks below and "
                "choose the matching setup if needed."
            ),
            local_stack_support_error=str(error),
            open_local_stack=True,
            status_code=422,
        )
    return _render_target(
        request,
        context,
        workspace_state,
        error=str(error),
        status_code=422,
        open_local_stack=True,
    )


async def _validate_selected_local_connection(
    context: WebContext,
    workspace_state,
    status: LocalStackStatus | None = None,
) -> LocalStackStatus:
    current = status
    if current is None:
        current = await run_in_threadpool(
            context.local_stack.refresh,
            workspace_state.workspace_id,
        )
    local_profile = _selected_local_profile(context, workspace_state)
    if local_profile is None:
        raise LocalStackError(
            "Choose and validate odoo.conf before testing database access."
        )
    blocked_checks = tuple(
        check.label
        for check in current.checks
        if check.key != "api" and check.level is not ReadinessLevel.READY
    )
    if blocked_checks:
        raise LocalStackError(
            "Local connection checks failed: "
            f"{', '.join(blocked_checks)}."
        )
    fingerprint = await run_in_threadpool(
        context.local_odoo_reader.get_target_fingerprint,
        workspace_state,
        local_profile,
    )
    return context.local_stack.mark_connection_ready(
        workspace_state.workspace_id,
        database=fingerprint.database,
        odoo_version=fingerprint.odoo_version,
    )


def build_target_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/workspaces/{workspace_id}/target/read-credential/quick"
    )
    async def save_quick_read_credential(
        request: Request,
        workspace_id: str,
    ):
        """Save one target-bound read key and retain the current workflow page."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "read_api_key",
                "read_api_key_storage",
                "return_to",
            },
        )
        return_to = f"/workspaces/{workspace_id}/target"
        json_request = _accepts_json(request)
        try:
            return_to = _quick_credential_return_to(form, workspace_id)
            workspace_state = context.queries.get(workspace_id)
            if workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE:
                raise SecretStoreError(
                    "A read-only API key is only needed for Remote Odoo"
                )
            context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PROJECT_EDIT,
            )
            credential_owner = context.target_credential_workspace(workspace_id)
            credential = store_target_credential(
                context.secret_store,
                credential_owner,
                TargetCredentialRole.READ,
                _text(form, "read_api_key"),
                persistent=_target_read_key_persistence(form),
            )
            audit_stored_target_credential(
                context.workspace_states,
                credential_owner,
                TargetCredentialRole.READ,
                credential,
                actor=context.actor,
            )
            context.remote_connections.clear(credential_owner.workspace_id)
            if credential_owner.workspace_id != workspace_id:
                context.remote_connections.clear(workspace_id)
        except (SecretStoreError, WorkspaceError) as error:
            if json_request:
                return JSONResponse({"detail": str(error)}, status_code=422)
            request.session["read_credential_error"] = str(error)
            return RedirectResponse(return_to, status_code=303)
        message = (
            "The read-only Odoo key is saved on this computer."
            if credential.persistent
            else "The read-only Odoo key is available until Impodo closes."
        )
        if json_request:
            return JSONResponse({"message": message, "return_to": return_to})
        _flash(request, message)
        return RedirectResponse(return_to, status_code=303)

    @router.get("/workspaces/{workspace_id}/target", response_class=HTMLResponse)
    async def workspace_target_form(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.status is WorkspaceStatus.CLOSED:
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/summary",
                status_code=303,
            )
        if (
            workspace_state.status is WorkspaceStatus.DRAFT
            and workspace_state.source_mode is SourceMode.FILE
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/files",
                status_code=303,
            )
        if workspace_state.status is WorkspaceStatus.DRAFT:
            blocked = blocking_setup_url(workspace_state, WorkspaceSetupStep.TARGET)
            if blocked is not None:
                return RedirectResponse(blocked, status_code=303)
        return _render_target(
            request,
            context,
            workspace_state,
            open_local_stack=request.query_params.get("local_stack") == "1",
        )

    @router.post("/workspaces/{workspace_id}/local-stack/select-config")
    async def select_local_stack_config(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "return_to"})
        workspace_state = context.queries.get(workspace_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        try:
            return_to = _local_stack_return_to(form)
            _require_local_stack_access(context, workspace_state)
            selected = context.local_stack.pick_config()
            if selected is None:
                _flash(request, "No local Odoo setup was selected.")
            else:
                status = await run_in_threadpool(
                    context.local_stack.select_config,
                    workspace_id,
                    selected,
                )
                if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                    await _validate_selected_local_connection(
                        context,
                        workspace_state,
                        status,
                    )
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                workspace_state,
                error,
                return_to=return_to,
            )
        return RedirectResponse(
            _local_stack_return_location(workspace_id, return_to),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/local-stack/refresh")
    async def refresh_local_stack(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "return_to"})
        workspace_state = context.queries.get(workspace_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        try:
            return_to = _local_stack_return_to(form)
            _require_local_stack_access(context, workspace_state)
            status = await run_in_threadpool(
                context.local_stack.refresh,
                workspace_id,
            )
            if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                await _validate_selected_local_connection(
                    context,
                    workspace_state,
                    status,
                )
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                workspace_state,
                error,
                return_to=return_to,
            )
        return RedirectResponse(
            _local_stack_return_location(workspace_id, return_to),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/local-stack/start")
    async def start_local_stack(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "confirm_start", "return_to"},
        )
        workspace_state = context.queries.get(workspace_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        try:
            return_to = _local_stack_return_to(form)
            _require_local_stack_start(context, workspace_state)
            if _text(form, "confirm_start") != "1":
                raise LocalStackError(
                    "Confirm the detected paths before starting the local stack."
                )
            status = await run_in_threadpool(
                context.local_stack.start,
                workspace_id,
            )
            if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                await _validate_selected_local_connection(
                    context,
                    workspace_state,
                    status,
                )
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                workspace_state,
                error,
                return_to=return_to,
            )
        _flash(request, "The local Odoo check is complete.")
        return RedirectResponse(
            _local_stack_return_location(workspace_id, return_to),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/local-stack/control")
    async def control_local_stack(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "confirm_control", "action", "return_to"},
        )
        workspace_state = context.queries.get(workspace_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        action = _text(form, "action")
        try:
            return_to = _local_stack_return_to(form)
            if _text(form, "confirm_control") != "1":
                raise LocalStackError(
                    "Confirm control of the Impodo-managed services first."
                )
            if action == "stop":
                _require_local_stack_stop(context, workspace_state)
                await run_in_threadpool(context.local_stack.stop, workspace_id)
                message = "The local Odoo services started by Impodo were stopped."
            elif action == "restart":
                _require_local_stack_stop(context, workspace_state)
                _require_local_stack_start(context, workspace_state)
                status = await run_in_threadpool(
                    context.local_stack.restart,
                    workspace_id,
                )
                if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                    await _validate_selected_local_connection(
                        context,
                        workspace_state,
                        status,
                    )
                message = "The local Odoo services started by Impodo were restarted."
            else:
                raise LocalStackError("Choose Stop or Restart.")
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                workspace_state,
                error,
                return_to=return_to,
            )
        _flash(request, message)
        return RedirectResponse(
            _local_stack_return_location(workspace_id, return_to),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/target")
    async def workspace_target(request: Request, workspace_id: str):
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
                "intended_applications",
                "read_api_key",
                "read_api_key_storage",
                "keep_api_key_for_loading",
                "remember_read_api_key",
                "api_key",
                "remember_api_key",
                "action",
            },
        )
        current = context.queries.get(workspace_id)
        if current.status is WorkspaceStatus.CLOSED:
            return RedirectResponse(
                f"/workspaces/{current.workspace_id}/summary",
                status_code=303,
            )
        if (
            current.status is WorkspaceStatus.DRAFT
            and current.source_mode is SourceMode.FILE
        ):
            return RedirectResponse(
                f"/workspaces/{current.workspace_id}/files",
                status_code=303,
            )
        if current.status is WorkspaceStatus.DRAFT:
            blocked = blocking_setup_url(current, WorkspaceSetupStep.TARGET)
            if blocked is not None:
                return RedirectResponse(blocked, status_code=303)
        purpose = _connection_purpose(current)
        local_test_requested = False
        remote_test_requested = False
        shared_test_requested = False
        show_local_results = False
        try:
            previous_workspace_state = context.queries.get(workspace_id)
            workspace_state = context.workspace_states.update_target(
                workspace_id,
                actor=context.actor,
                expected_revision=_revision(form),
                odoo_connection_mode=_text(form, "odoo_connection_mode"),
                odoo_base_url=_text(form, "odoo_base_url"),
                odoo_database=_text(form, "odoo_database"),
                intended_applications=form.getlist("intended_applications"),
                intended_models=(
                    context.test_runs.required_models_for_workspace(
                        workspace_id,
                        actor=context.actor,
                    )
                    or None
                ),
            )
            target_changed = (
                target_read_credential_id(previous_workspace_state)
                != target_read_credential_id(workspace_state)
                or target_write_credential_id(previous_workspace_state)
                != target_write_credential_id(workspace_state)
            )
            if target_changed:
                removal_receipts = delete_target_credentials(
                    context.secret_store,
                    previous_workspace_state,
                    reason=TargetCredentialRemovalReason.TARGET_CHANGED,
                )
                audit_removed_target_credentials(
                    context.workspace_states,
                    previous_workspace_state,
                    removal_receipts,
                    actor=context.actor,
                )
                context.remote_connections.clear(workspace_id)
            action = _text(form, "action")
            local_test_requested = (
                action == "test"
                and workspace_state.odoo_connection_mode is OdooConnectionMode.LOCAL
            )
            remote_test_requested = (
                action == "test"
                and workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
            )
            shared_test_requested = action == "test" and (
                remote_test_requested or workspace_state.source_mode is SourceMode.ODOO
            )
            submitted_key = _text(form, "read_api_key") or _text(
                form,
                "api_key",
            )
            keep_key_for_loading = "keep_api_key_for_loading" in form
            if keep_key_for_loading:
                access_context = context.workspace_access.resolve(
                    workspace_id,
                    actor=context.actor,
                    capability=Capability.EXPORT_PLAN_EXECUTE,
                )
                data_version = context.data_versions.get(
                    access_context.data_version_id,
                    actor=context.actor,
                )
                if data_version.purpose.value == "PRODUCTION":
                    raise SecretStoreError(
                        "Production requires a separate limited write key"
                    )
            if submitted_key:
                context.remote_connections.clear(workspace_id)
                read_credential = store_target_credential(
                    context.secret_store,
                    workspace_state,
                    TargetCredentialRole.READ,
                    submitted_key,
                    persistent=_target_read_key_persistence(form),
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    workspace_state,
                    TargetCredentialRole.READ,
                    read_credential,
                    actor=context.actor,
                )
            else:
                read_credential = get_target_credential(
                    context.secret_store,
                    workspace_state,
                    TargetCredentialRole.READ,
                )
            if keep_key_for_loading:
                if read_credential is None:
                    raise SecretStoreError(
                        "Enter or save an Odoo API key before keeping it for loading"
                    )
                write_credential = store_target_credential(
                    context.secret_store,
                    workspace_state,
                    TargetCredentialRole.WRITE,
                    read_credential.secret,
                    persistent=read_credential.persistent,
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    workspace_state,
                    TargetCredentialRole.WRITE,
                    write_credential,
                    actor=context.actor,
                )
            if action == "test":
                local_profile = _selected_local_profile(context, workspace_state)
                if local_profile is not None:
                    if workspace_state.source_mode is SourceMode.ODOO and read_credential is None:
                        raise OdooReadCredentialMissingError(
                            "Enter a read-only Odoo API key before checking an Odoo source."
                        )
                    show_local_results = True
                    await _validate_selected_local_connection(
                        context,
                        workspace_state,
                    )
                    if workspace_state.source_mode is SourceMode.ODOO:
                        result = await run_in_threadpool(
                            context.odoo_connection_tests.test_read,
                            workspace_state,
                            read_credential.secret,
                            purpose=purpose,
                        )
                        context.remote_connections.mark_checked(
                            workspace_state,
                            result.fingerprint,
                            result.read_identity,
                            purpose=purpose,
                        )
                else:
                    if read_credential is None:
                        if (
                            workspace_state.odoo_connection_mode
                            is OdooConnectionMode.LOCAL
                        ):
                            raise OdooReadCredentialMissingError(
                                "Local mode does not require an API key. "
                                "Choose and validate odoo.conf with the local "
                                "readiness assistant first."
                            )
                        raise OdooReadCredentialMissingError(
                            "Enter an Odoo API key for this exact remote target "
                            "to test"
                        )
                    result = await run_in_threadpool(
                        context.odoo_connection_tests.test_read,
                        workspace_state,
                        read_credential.secret,
                        purpose=purpose,
                    )
                    context.remote_connections.mark_checked(
                        workspace_state,
                        result.fingerprint,
                        result.read_identity,
                        purpose=purpose,
                    )
                target_url = f"/workspaces/{workspace_id}/target"
                if local_test_requested:
                    _flash(
                        request,
                        "The Odoo connection is ready. Nothing was changed.",
                    )
                if show_local_results:
                    target_url = f"{target_url}?local_stack=1"
                elif remote_test_requested:
                    target_url = f"{target_url}#remote-connection-status"
                return RedirectResponse(
                    target_url,
                    status_code=303,
                )
        except (
            WorkspaceStateError,
            SecretStoreError,
            ConnectorError,
            LocalStackError,
            WorkspaceError,
        ) as error:
            if local_test_requested:
                context.local_stack.mark_connection_error(
                    workspace_id,
                    detail=str(error),
                )
            if shared_test_requested:
                workspace_state = context.queries.get(workspace_id)
                context.remote_connections.mark_error(
                    workspace_state,
                    error,
                    purpose=purpose,
                )
                if remote_test_requested:
                    return RedirectResponse(
                        f"/workspaces/{workspace_id}/target#remote-connection-status",
                        status_code=303,
                    )
            return _render_target(
                request,
                context,
                context.queries.get(workspace_id),
                error=str(error),
                status_code=422,
                open_local_stack=local_test_requested,
            )
        if workspace_setup_requirements_for_step(
            workspace_state,
            WorkspaceSetupStep.TARGET,
        ):
            return _render_target(
                request,
                context,
                workspace_state,
                setup_attention_requested=True,
                status_code=422,
            )
        local_ready = (
            workspace_state.odoo_connection_mode is OdooConnectionMode.LOCAL
            and context.local_stack.get(workspace_state.workspace_id).metadata_ready
        )
        if local_ready and workspace_state.source_mode is SourceMode.ODOO:
            local_ready = context.remote_connections.get(workspace_state, purpose).ready
        remote_ready = (
            workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
            and context.remote_connections.get(workspace_state, purpose).ready
        )
        if not (local_ready or remote_ready):
            return _render_target(
                request,
                context,
                workspace_state,
                error="Check the Odoo connection before continuing. Nothing was changed in Odoo.",
                status_code=422,
            )
        if workspace_state.source_mode is SourceMode.ODOO and read_credential is None:
            return _render_target(
                request,
                context,
                workspace_state,
                error="Enter a read-only Odoo API key before continuing.",
                status_code=422,
            )
        if workspace_state.status is WorkspaceStatus.DRAFT:
            try:
                workspace_state = context.workspace_states.register(
                    workspace_state.workspace_id,
                    actor=context.actor,
                    expected_revision=workspace_state.revision,
                )
            except WorkspaceStateError as error:
                return _render_target(
                    request,
                    context,
                    workspace_state,
                    error=str(error),
                    status_code=422,
                )
        return RedirectResponse(
            f"/workspaces/{workspace_state.workspace_id}/schema",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/target/read-credential/delete")
    async def forget_target_read_credential(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.status is WorkspaceStatus.CLOSED:
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/summary",
                status_code=303,
            )
        try:
            context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PROJECT_EDIT,
            )
            receipt = delete_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.READ,
                reason=TargetCredentialRemovalReason.USER_REQUESTED,
            )
            if receipt is not None:
                audit_removed_target_credentials(
                    context.workspace_states,
                    workspace_state,
                    (receipt,),
                    actor=context.actor,
                )
            context.remote_connections.clear(workspace_id)
        except SecretStoreError as error:
            return _render_target(
                request,
                context,
                workspace_state,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "The read-only Odoo key was forgotten. Nothing was changed in Odoo.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/target#read-credential-status",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/target/write-credential/delete")
    async def forget_target_write_credential(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.status is WorkspaceStatus.CLOSED:
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/summary",
                status_code=303,
            )
        try:
            context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.EXPORT_PLAN_EXECUTE,
            )
            receipt = delete_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.WRITE,
                reason=TargetCredentialRemovalReason.USER_REQUESTED,
            )
            if receipt is not None:
                audit_removed_target_credentials(
                    context.workspace_states,
                    workspace_state,
                    (receipt,),
                    actor=context.actor,
                )
        except SecretStoreError as error:
            return _render_target(
                request,
                context,
                workspace_state,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "The Odoo loading key was forgotten. Nothing was changed in Odoo.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/target#read-credential-status",
            status_code=303,
        )

    return router
