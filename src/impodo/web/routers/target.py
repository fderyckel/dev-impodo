"""Target browser routes."""

from __future__ import annotations
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...connectors import ConnectorError
from ...local_stack import LocalStackError, LocalStackStatus, ReadinessLevel
from ...projects import OdooConnectionMode, ProjectError
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash
from ..presenters.mapping_forms import _draft_or_redirect
from ..presenters.summary import (
    _render_summary,
    _render_target,
    _require_local_stack_access,
    _require_local_stack_start,
    _require_local_stack_stop,
)
from ..target_readers import _selected_local_profile
from ..target_credentials import (
    TargetCredentialRole,
    TargetCredentialRemovalReason,
    audit_removed_target_credentials,
    audit_stored_target_credential,
    delete_target_credentials,
    get_target_credential,
    store_target_credential,
    target_read_credential_id,
    target_write_credential_id,
)


_LOCAL_STACK_RETURN_TARGET = "target"
_LOCAL_STACK_RETURN_SUMMARY_COMPARE = "summary_compare"
_LOCAL_STACK_RETURN_VALUES = {
    _LOCAL_STACK_RETURN_TARGET,
    _LOCAL_STACK_RETURN_SUMMARY_COMPARE,
}


def _local_stack_return_to(form) -> str:
    value = _text(form, "return_to") or _LOCAL_STACK_RETURN_TARGET
    if value not in _LOCAL_STACK_RETURN_VALUES:
        raise LocalStackError("The requested local Odoo return step is unavailable.")
    return value


def _local_stack_return_location(project_id: str, return_to: str) -> str:
    if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
        return (
            f"/projects/{project_id}/summary?local_stack=1"
            "#compare-with-odoo"
        )
    return f"/projects/{project_id}/target?local_stack=1"


def _render_local_stack_error(
    request: Request,
    context: WebContext,
    project,
    error: Exception,
    *,
    return_to: str,
):
    if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
        context.local_stack.mark_connection_error(
            project.project_id,
            detail=str(error),
        )
        return _render_summary(
            request,
            context,
            project.project_id,
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
        project,
        error=str(error),
        status_code=422,
        open_local_stack=True,
    )


async def _validate_selected_local_connection(
    context: WebContext,
    project,
    status: LocalStackStatus | None = None,
) -> LocalStackStatus:
    current = status
    if current is None:
        current = await run_in_threadpool(
            context.local_stack.refresh,
            project.project_id,
        )
    local_profile = _selected_local_profile(context, project)
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
        project,
        local_profile,
    )
    return context.local_stack.mark_connection_ready(
        project.project_id,
        database=fingerprint.database,
        odoo_version=fingerprint.odoo_version,
    )


def build_target_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{project_id}/target", response_class=HTMLResponse)
    async def project_target_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render_target(
            request,
            context,
            project,
            open_local_stack=request.query_params.get("local_stack") == "1",
        )

    @router.post("/projects/{project_id}/local-stack/select-config")
    async def select_local_stack_config(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "return_to"})
        project = context.queries.get(project_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        try:
            return_to = _local_stack_return_to(form)
            _require_local_stack_access(context, project)
            selected = context.local_stack.pick_config()
            if selected is None:
                _flash(request, "No local Odoo setup was selected.")
            else:
                status = await run_in_threadpool(
                    context.local_stack.select_config,
                    project_id,
                    selected,
                )
                if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                    await _validate_selected_local_connection(
                        context,
                        project,
                        status,
                    )
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                project,
                error,
                return_to=return_to,
            )
        return RedirectResponse(
            _local_stack_return_location(project_id, return_to),
            status_code=303,
        )

    @router.post("/projects/{project_id}/local-stack/refresh")
    async def refresh_local_stack(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "return_to"})
        project = context.queries.get(project_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        try:
            return_to = _local_stack_return_to(form)
            _require_local_stack_access(context, project)
            status = await run_in_threadpool(
                context.local_stack.refresh,
                project_id,
            )
            if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                await _validate_selected_local_connection(
                    context,
                    project,
                    status,
                )
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                project,
                error,
                return_to=return_to,
            )
        return RedirectResponse(
            _local_stack_return_location(project_id, return_to),
            status_code=303,
        )

    @router.post("/projects/{project_id}/local-stack/start")
    async def start_local_stack(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "confirm_start", "return_to"},
        )
        project = context.queries.get(project_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        try:
            return_to = _local_stack_return_to(form)
            _require_local_stack_start(context, project)
            if _text(form, "confirm_start") != "1":
                raise LocalStackError(
                    "Confirm the detected paths before starting the local stack."
                )
            status = await run_in_threadpool(
                context.local_stack.start,
                project_id,
            )
            if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                await _validate_selected_local_connection(
                    context,
                    project,
                    status,
                )
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                project,
                error,
                return_to=return_to,
            )
        _flash(request, "The local Odoo check is complete.")
        return RedirectResponse(
            _local_stack_return_location(project_id, return_to),
            status_code=303,
        )

    @router.post("/projects/{project_id}/local-stack/control")
    async def control_local_stack(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "confirm_control", "action", "return_to"},
        )
        project = context.queries.get(project_id)
        return_to = _LOCAL_STACK_RETURN_TARGET
        action = _text(form, "action")
        try:
            return_to = _local_stack_return_to(form)
            if _text(form, "confirm_control") != "1":
                raise LocalStackError(
                    "Confirm control of the Impodo-managed services first."
                )
            if action == "stop":
                _require_local_stack_stop(context, project)
                await run_in_threadpool(context.local_stack.stop, project_id)
                message = "The local Odoo services started by Impodo were stopped."
            elif action == "restart":
                _require_local_stack_stop(context, project)
                _require_local_stack_start(context, project)
                status = await run_in_threadpool(
                    context.local_stack.restart,
                    project_id,
                )
                if return_to == _LOCAL_STACK_RETURN_SUMMARY_COMPARE:
                    await _validate_selected_local_connection(
                        context,
                        project,
                        status,
                    )
                message = "The local Odoo services started by Impodo were restarted."
            else:
                raise LocalStackError("Choose Stop or Restart.")
        except (ConnectorError, LocalStackError, WorkspaceError) as error:
            return _render_local_stack_error(
                request,
                context,
                project,
                error,
                return_to=return_to,
            )
        _flash(request, message)
        return RedirectResponse(
            _local_stack_return_location(project_id, return_to),
            status_code=303,
        )

    @router.post("/projects/{project_id}/target")
    async def project_target(request: Request, project_id: str):
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
                "remember_read_api_key",
                "api_key",
                "remember_api_key",
                "action",
            },
        )
        local_test_requested = False
        remote_test_requested = False
        show_local_results = False
        try:
            previous_project = context.queries.get(project_id)
            project = context.projects.update_target(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                odoo_connection_mode=_text(form, "odoo_connection_mode"),
                odoo_base_url=_text(form, "odoo_base_url"),
                odoo_database=_text(form, "odoo_database"),
                intended_applications=form.getlist("intended_applications"),
            )
            target_changed = (
                target_read_credential_id(previous_project)
                != target_read_credential_id(project)
                or target_write_credential_id(previous_project)
                != target_write_credential_id(project)
            )
            if target_changed:
                removal_receipts = delete_target_credentials(
                    context.secret_store,
                    previous_project,
                    reason=TargetCredentialRemovalReason.TARGET_CHANGED,
                )
                audit_removed_target_credentials(
                    context.projects,
                    previous_project,
                    removal_receipts,
                    actor=context.actor,
                )
                context.remote_connections.clear(project_id)
            action = _text(form, "action")
            local_test_requested = (
                action == "test"
                and project.odoo_connection_mode is OdooConnectionMode.LOCAL
            )
            remote_test_requested = (
                action == "test"
                and project.odoo_connection_mode is OdooConnectionMode.REMOTE
            )
            submitted_key = _text(form, "read_api_key") or _text(
                form,
                "api_key",
            )
            if submitted_key:
                context.remote_connections.clear(project_id)
                read_credential = store_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.READ,
                    submitted_key,
                    persistent=(
                        "remember_read_api_key" in form
                        or "remember_api_key" in form
                    ),
                )
                audit_stored_target_credential(
                    context.projects,
                    project,
                    TargetCredentialRole.READ,
                    read_credential,
                    actor=context.actor,
                )
            else:
                read_credential = get_target_credential(
                    context.secret_store,
                    project,
                    TargetCredentialRole.READ,
                )
            if action == "test":
                local_profile = _selected_local_profile(context, project)
                if local_profile is not None:
                    show_local_results = True
                    await _validate_selected_local_connection(
                        context,
                        project,
                    )
                else:
                    if read_credential is None:
                        if (
                            project.odoo_connection_mode
                            is OdooConnectionMode.LOCAL
                        ):
                            raise SecretStoreError(
                                "Local mode does not require an API key. "
                                "Choose and validate odoo.conf with the local "
                                "readiness assistant first."
                            )
                        raise SecretStoreError(
                            "Enter an Odoo API key for this exact remote target "
                            "to test"
                        )
                    fingerprint = await run_in_threadpool(
                        context.connection_tester,
                        project,
                        read_credential.secret,
                    )
                    if remote_test_requested:
                        identity = await run_in_threadpool(
                            context.read_identity_probe,
                            project,
                            read_credential.secret,
                            ("res.partner",),
                        )
                        context.remote_connections.mark_checked(
                            project,
                            fingerprint,
                            identity,
                        )
                target_url = f"/projects/{project_id}/target"
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
            ProjectError,
            SecretStoreError,
            ConnectorError,
            LocalStackError,
            WorkspaceError,
        ) as error:
            if local_test_requested:
                context.local_stack.mark_connection_error(
                    project_id,
                    detail=str(error),
                )
            if remote_test_requested:
                project = context.queries.get(project_id)
                context.remote_connections.mark_error(project, error)
                return RedirectResponse(
                    f"/projects/{project_id}/target#remote-connection-status",
                    status_code=303,
                )
            return _render_target(
                request,
                context,
                context.queries.get(project_id),
                error=str(error),
                status_code=422,
                open_local_stack=local_test_requested,
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/review",
            status_code=303,
        )

    return router
