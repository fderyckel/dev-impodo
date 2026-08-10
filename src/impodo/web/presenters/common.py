"""Common web helpers."""

from __future__ import annotations

import re

from fastapi import Request

from ..context import WebContext
from .navigation import build_project_navigation


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
        context.setdefault("support_error", support_error)
    project = context.get("project")
    if project is not None and "project_navigation" not in context:
        context["project_navigation"] = build_project_navigation(
            request.app.state.context,
            project,
            template_name,
            current_path=request.url.path,
        )
    values = {
        "csrf_token": request.session.get("csrf_token", ""),
        "flash": request.session.pop("flash", None),
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
    if (
        not has_technical_code
        and not concurrency_message
        and not any(item in lowered for item in technical_markers)
    ):
        return raw, None

    if "csrf" in lowered:
        plain = "This page has expired. Refresh it, review your choices, and try again."
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
        plain = "The saved source information no longer matches this project. Check the source files again before continuing."
    else:
        plain = "Impodo could not complete this action. Your saved project information is unchanged; review the page and try again."
    return plain, raw


def _project_error(
    request: Request,
    context: WebContext,
    project_id: str,
    template_name: str,
    error: Exception,
    **extra,
):
    project = context.queries.get(project_id)
    return _render(
        request,
        template_name,
        project=project,
        error=str(error),
        status_code=422,
        **extra,
    )


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message
