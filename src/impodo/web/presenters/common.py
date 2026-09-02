"""Common web helpers."""

from __future__ import annotations

import re

from fastapi import Request

from impodo.domain.shared.access import Capability
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
    WorkspaceStatus,
)
from ..context import WebContext
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential_status,
)
from .concepts import CONCEPTS, CONCEPTS_BY_SLUG
from .navigation import build_workspace_navigation
from .setup import build_workspace_setup_view


def _render(
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    **context,
):
    raw_error = context.get("error")
    if raw_error:
        plain_error, support_error = _plain_ui_error(str(raw_error))
        context["error"] = plain_error
        if context.get("support_error") is None:
            context["support_error"] = support_error
    workspace_state = context.get("workspace_state")
    application = request.app.state.context
    prompt_error = request.session.pop("read_credential_error", None)
    if (
        isinstance(workspace_state, WorkspaceState)
        and workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
        and not context.get("disable_default_read_credential_prompt")
    ):
        credential_owner = application.target_credential_workspace(
            workspace_state.workspace_id,
            workspace_state=workspace_state,
        )
        access = getattr(request.state, "workspace_access_context", None)
        if access is None or access.workspace_id != workspace_state.workspace_id:
            access = application.workspace_access.resolve(
                workspace_state.workspace_id,
                actor=application.actor,
                capability=Capability.PROJECT_VIEW,
            )
        credential_status = get_target_credential_status(
            application.secret_store,
            credential_owner,
            TargetCredentialRole.READ,
        )
        query = f"?{request.url.query}" if request.url.query else ""
        explicitly_required = bool(context.get("read_credential_required"))
        context["read_credential_prompt"] = {
            "action_href": (
                f"/projects/{access.project_id}/"
                f"workspaces/{workspace_state.workspace_id}/"
                "target/read-credential/quick"
            ),
            "auto_open": explicitly_required or prompt_error is not None,
            "error": prompt_error,
            "required": (
                explicitly_required
                or (raw_error is not None and not credential_status.available)
            ),
            "resume": str(context.get("read_credential_resume", "stay")),
            "resume_action": str(
                context.get("read_credential_resume_action", "")
            ),
            "return_to": f"{request.url.path}{query}",
            "status_label": credential_status.label,
        }
    workspace_view = None
    if isinstance(workspace_state, WorkspaceState) and (
        "workspace_navigation" not in context or "migration_context" not in context
    ):
        workspace_view = application.workspace_views.get(
            workspace_state.workspace_id,
            actor=application.actor,
        )
    if (
        isinstance(workspace_state, WorkspaceState)
        and "workspace_navigation" not in context
    ):
        assert workspace_view is not None
        context["workspace_navigation"] = build_workspace_navigation(
            application,
            workspace_state,
            template_name,
            current_path=request.url.path,
            migration_project_name=workspace_view.migration_project.display_name,
            workspace_view=workspace_view,
        )
    if (
        isinstance(workspace_state, WorkspaceState)
        and workspace_state.status is WorkspaceStatus.DRAFT
    ):
        setup_view = build_workspace_setup_view(workspace_state, template_name)
        context.setdefault("setup_steps", setup_view.steps)
        context.setdefault(
            "setup_current_requirements",
            setup_view.current_requirements,
        )
        context.setdefault("setup_recovery_steps", setup_view.recovery_steps)
        context["setup_attention_requested"] = bool(
            context.get("setup_attention_requested")
            or request.query_params.get("blocked") == "1"
        )
    if (
        isinstance(workspace_state, WorkspaceState)
        and "migration_context" not in context
    ):
        assert workspace_view is not None
        context["workspace_view"] = workspace_view
        context["workspace_id"] = workspace_view.migration_workspace.workspace_id
        context["migration_project"] = workspace_view.migration_project
        context["migration_workspace"] = workspace_view.migration_workspace
        context["data_version"] = workspace_view.data_version
        context["migration_run"] = workspace_view.migration_run
        context["migration_context"] = workspace_view
    values = {
        "csrf_token": request.session.get("csrf_token", ""),
        "diagnostics_available": (
            request.app.state.diagnostic_recorder is not None
        ),
        "flash": request.session.pop("flash", None),
        "concepts": CONCEPTS,
        "concepts_by_slug": CONCEPTS_BY_SLUG,
        **context,
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template_name,
        context=values,
        status_code=status_code,
    )


def _plain_ui_error(message: str) -> tuple[str, str | None]:
    """Keep implementation details out of the default data-manager message."""
    raw = message.strip()
    if not raw:
        return "Please review the page and try again.", None

    lowered = raw.lower()
    technical_markers = (
        "sha256",
        "hash",
        "uuid",
        "run_id",
        "lifecycle_version",
        "dataset_id",
        "catalog",
        "schema",
        "snapshot",
        "traceback",
        "valueerror",
        "keyerror",
        "http ",
        "odoo.conf",
        "127.0.0.1",
        "postgres",
    )
    has_technical_code = bool(re.search(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){1,}\b", raw))
    concurrency_message = any(
        item in lowered
        for item in ("changed in another request", "reload before", "out of date")
    )
    stored_quality_error = "stored quality evidence" in lowered
    if (
        not has_technical_code
        and not concurrency_message
        and not stored_quality_error
        and not any(item in lowered for item in technical_markers)
    ):
        return raw, None

    if "csrf" in lowered:
        plain = "This page has expired. Refresh it, review your choices, and try again."
    elif stored_quality_error:
        plain = (
            "Impodo could not reopen the saved prepared-data review. "
            "Nothing was sent to Odoo and your saved project was not changed. "
            "Restart Impodo and try Compare with Odoo again. If it still fails, "
            "contact support."
        )
    elif concurrency_message or any(
        item in lowered for item in ("revision", "version", "stale", "changed since")
    ):
        plain = "This page is out of date. Reload it, review the latest information, and try again."
    elif any(item in lowered for item in ("odoo", "connection", "database", "postgres")):
        plain = "Impodo could not check Odoo. Confirm that Odoo is available and review the connection details before trying again."
    elif any(item in lowered for item in ("mapping", "field", "business key")):
        plain = "Impodo could not check these data matches. Review the selected fields and try again."
    elif any(
        item in lowered
        for item in (
            "artifact_path_too_long",
            "immutable source snapshot",
            "immutable prepared snapshot",
        )
    ):
        plain = (
            "Impodo could not save the protected copy of these tables. "
            "Your source files and saved project are unchanged. "
            "Contact support before trying again."
        )
    elif any(item in lowered for item in ("source", "file", "table", "dataset", "catalog")):
        plain = "The saved source information no longer matches this Data version. Check the source files again before continuing."
    else:
        plain = "Impodo could not complete this action. Your saved project information is unchanged; review the page and try again."
    return plain, raw


def _workspace_error(
    request: Request,
    context: WebContext,
    workspace_id: str,
    template_name: str,
    error: Exception,
    **extra,
):
    workspace_state = context.queries.get(workspace_id)
    return _render(
        request,
        template_name,
        workspace_state=workspace_state,
        error=str(error),
        status_code=422,
        **extra,
    )


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message
