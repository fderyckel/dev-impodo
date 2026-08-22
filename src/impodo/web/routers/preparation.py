"""Responsive browser routes for session-scoped background preparation jobs."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...application.preparation_job_registry import (
    PreparationJobNotFoundError,
    PreparationJobStateError,
)
from ...migration_foundation import MigrationFoundationError
from ...preparation_jobs import PreparationJob, PreparationJobStatus
from ...preparation_jobs import PreparationWorkspace
from ...workspace_errors import WorkspaceError
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash, _render
from ..presenters.navigation import build_preparation_job_navigation
from ..security import require_session


def build_preparation_router(context: WebContext) -> APIRouter:
    """Build enqueue, progress, cancellation, and retry routes."""

    router = APIRouter()

    @router.get("/workspaces/{project_id}/prepare", response_class=HTMLResponse)
    async def prepare_project_data(request: Request, project_id: str):
        require_session(request)
        active = (
            context.preparation_jobs.active(project_id)
            if context.preparation_jobs is not None
            else None
        )
        if active is not None:
            return RedirectResponse(
                _progress_url(project_id, active.job_id),
                status_code=303,
            )
        try:
            _preparation_workspace(context, project_id)
        except WorkspaceError as error:
            request.session["summary_error"] = str(error)
            return RedirectResponse(
                f"/workspaces/{project_id}/summary",
                status_code=303,
            )
        project = context.queries.get(project_id)
        resolution = context.resolution.current_summary(project_id)
        if resolution is not None and resolution.status != "FROZEN":
            return RedirectResponse(
                f"/workspaces/{project_id}/resolution",
                status_code=303,
            )
        normalization = context.normalization.current_summary(project_id)
        if normalization is not None:
            return RedirectResponse(
                f"/workspaces/{project_id}/normalization",
                status_code=303,
            )
        revision = context.queries.get_mapping_revision(project_id)
        submission = (
            context.queries.get_mapping_submission(project_id, revision.version)
            if revision is not None
            else None
        )
        can_prepare = bool(
            revision is not None
            and submission is not None
            and submission.mapping_content_hash
            == revision.definition.content_hash
        )
        return _render(
            request,
            "project_prepare.html",
            project=project,
            can_prepare=can_prepare,
        )

    @router.post("/workspaces/{project_id}/summary/check")
    async def check_project_data(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            job = enqueue_preparation(context, project_id)
        except WorkspaceError as error:
            request.session["summary_error"] = str(error)
            return RedirectResponse(
                f"/workspaces/{project_id}/summary",
                status_code=303,
            )
        request.session.pop("summary_error", None)
        _flash(request, "Preparation started. You can follow each step here.")
        return RedirectResponse(_progress_url(project_id, job.job_id), status_code=303)

    @router.get(
        "/workspaces/{project_id}/preparation/{job_id}",
        response_class=HTMLResponse,
    )
    async def preparation_progress(request: Request, project_id: str, job_id: str):
        require_session(request)
        return _render_progress(request, _get_job(context, project_id, job_id))

    @router.get("/workspaces/{project_id}/preparation/{job_id}/status")
    async def preparation_status(request: Request, project_id: str, job_id: str):
        require_session(request)
        return JSONResponse(_job_payload(_get_job(context, project_id, job_id)))

    @router.post("/workspaces/{project_id}/preparation/{job_id}/cancel")
    async def cancel_preparation(request: Request, project_id: str, job_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            _manager(context).cancel(project_id, job_id)
        except PreparationJobNotFoundError as error:
            raise HTTPException(status_code=404, detail="Preparation job not found") from error
        _flash(request, "Impodo will stop safely after the current source batch.")
        return RedirectResponse(_progress_url(project_id, job_id), status_code=303)

    @router.post("/workspaces/{project_id}/preparation/{job_id}/retry")
    async def retry_preparation(request: Request, project_id: str, job_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            workspace = _preparation_workspace(context, project_id)
            project = context.queries.get(project_id)
            job = _manager(context).retry(
                project_id,
                job_id,
                project.name,
                _preparation_row_count(context, project_id),
                actor=context.actor,
                workspace=workspace,
            )
        except PreparationJobNotFoundError as error:
            raise HTTPException(status_code=404, detail="Preparation job not found") from error
        except (PreparationJobStateError, WorkspaceError) as error:
            current = _get_job(context, project_id, job_id)
            return _render_progress(
                request,
                current,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Preparation started again.")
        return RedirectResponse(_progress_url(project_id, job.job_id), status_code=303)

    return router


def enqueue_preparation(context: WebContext, project_id: str) -> PreparationJob:
    """Capture lightweight display/scale metadata before starting the process."""

    workspace = _preparation_workspace(context, project_id)
    project = context.queries.get(project_id)
    total_rows = _preparation_row_count(context, project_id)
    return _manager(context).enqueue(
        project_id,
        project.name,
        total_rows,
        actor=context.actor,
        workspace=workspace,
    )


def _preparation_workspace(
    context: WebContext,
    project_id: str,
) -> PreparationWorkspace:
    """Resolve one open workspace and its Project-owned DataVersion."""

    try:
        workspace = context.migration_workspaces.get(
            project_id,
            actor=context.actor,
        )
        data_version = context.data_versions.get(
            workspace.data_version_id,
            actor=context.actor,
        )
        run = context.migration_runs.get(
            workspace.migration_run_id,
            actor=context.actor,
        )
        return PreparationWorkspace.from_context(workspace, data_version, run)
    except MigrationFoundationError as error:
        raise WorkspaceError(
            f"{error}. No Odoo records were changed. Return to the Project "
            "overview and continue from its current authoring workspace."
        ) from error


def _preparation_row_count(context: WebContext, project_id: str) -> int:
    """Return optional progress metadata without bypassing worker validation."""

    try:
        selection = context.queries.get_source_selection(project_id)
    except WorkspaceError:
        # The worker owns authoritative validation and records a durable failed
        # job. Display metadata must not prevent that governed failure path.
        selection = None
    return sum(item.row_count for item in selection.datasets) if selection else 0


def _render_progress(
    request: Request,
    job: PreparationJob,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    project = SimpleNamespace(
        project_id=job.project_id,
        name=job.project_name,
        registered_at=True,
    )
    return _render(
        request,
        "project_preparation_progress.html",
        project=project,
        project_navigation=build_preparation_job_navigation(job),
        migration_context={
            "project_id": job.workspace.project_id,
            "data_version_id": job.workspace.data_version_id,
            "data_version_number": job.workspace.data_version_number,
            "data_version_purpose": job.workspace.data_version_purpose.value,
            "migration_run_id": job.workspace.migration_run_id,
            "workspace_id": job.workspace.workspace_id,
        },
        job=job,
        job_payload=_job_payload(job),
        failure_message=job.failure_message,
        error=error,
        status_code=status_code,
    )


def _job_payload(job: PreparationJob) -> dict[str, object]:
    redirect_url = ""
    if job.status is PreparationJobStatus.SUCCEEDED:
        redirect_url = f"/workspaces/{job.project_id}/normalization"
    elif job.status is PreparationJobStatus.REVIEW_REQUIRED:
        redirect_url = f"/workspaces/{job.project_id}/resolution"
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "phase": job.phase.value,
        "message": job.message,
        "completed_rows": job.completed_rows,
        "total_rows": job.total_rows,
        "progress_percent": job.progress_percent,
        "cancel_requested": job.cancel_requested,
        "failure_code": job.failure_code,
        "failure_message": job.failure_message,
        "redirect_url": redirect_url,
    }


def _get_job(context: WebContext, project_id: str, job_id: str) -> PreparationJob:
    try:
        return _manager(context).get(project_id, job_id)
    except PreparationJobNotFoundError as error:
        raise HTTPException(status_code=404, detail="Preparation job not found") from error


def _manager(context: WebContext):
    if context.preparation_jobs is None:
        raise RuntimeError("Background preparation jobs are unavailable")
    return context.preparation_jobs


def _progress_url(project_id: str, job_id: str) -> str:
    return f"/workspaces/{project_id}/preparation/{job_id}"
