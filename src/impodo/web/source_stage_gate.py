"""Return browser users to the earliest incomplete source decision."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.workspace.source_readiness import (
    SourceStageIssue,
    SourceStageReadiness,
)

from .context import WebContext
from .presenters.common import _flash


async def source_stage_redirect(
    request: Request,
    context: WebContext,
    workspace_id: str,
) -> RedirectResponse | None:
    """Redirect a downstream browser request when source evidence is stale."""

    readiness = await run_in_threadpool(
        context.navigation.get_source_readiness,
        workspace_id,
    )
    if readiness.ready:
        return None
    _flash(request, source_stage_message(readiness))
    return RedirectResponse(
        source_stage_url(workspace_id, readiness),
        status_code=303,
    )


def source_stage_url(
    workspace_id: str,
    readiness: SourceStageReadiness,
) -> str:
    """Return the page containing the earliest incomplete source action."""

    if readiness.issue in {
        SourceStageIssue.SCHEMA_MISSING,
        SourceStageIssue.SCHEMA_ATTENTION,
    }:
        return f"/workspaces/{workspace_id}/schema"
    if readiness.issue is SourceStageIssue.CAPTURE_PLANS_INCOMPLETE:
        return f"/workspaces/{workspace_id}/sources#capture-plan"
    if readiness.issue is SourceStageIssue.FILE_SOURCE_NOT_FROZEN:
        return f"/workspaces/{workspace_id}/sources"
    return f"/workspaces/{workspace_id}/sources#current-capture"


def source_stage_message(readiness: SourceStageReadiness) -> str:
    """Explain why downstream work is locked and what remains protected."""

    if readiness.issue is SourceStageIssue.SCHEMA_MISSING:
        return (
            "Choose the Odoo record types and fields for this source before "
            "continuing."
        )
    if readiness.issue is SourceStageIssue.SCHEMA_ATTENTION:
        return (
            "The Odoo source details changed. Review and confirm the current "
            "record types and fields before continuing."
        )
    if readiness.issue is SourceStageIssue.CAPTURE_PLANS_INCOMPLETE:
        count = len(readiness.missing_capture_models)
        detail = (
            f" {count} selected record type{'s' if count != 1 else ''} still "
            "need a capture plan."
            if count
            else " The saved capture plans no longer match the selected record types."
        )
        return (
            "Your source choices changed."
            f"{detail} Complete every capture plan, then download and freeze "
            "a new source version before continuing. Any previous frozen "
            "version remains in history."
        )
    if readiness.issue is SourceStageIssue.FILE_SOURCE_NOT_FROZEN:
        return (
            "Confirm and save the source tables for this data version before "
            "continuing."
        )
    return (
        "Your source choices changed. Download and freeze a new source version "
        "before continuing. Any previous frozen version remains in history."
    )
