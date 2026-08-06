"""Responsive browser routes for session-scoped background preparation jobs."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...application.preparation_job_registry import (
    PreparationJobNotFoundError,
    PreparationJobStateError,
)
from ...preparation_jobs import PreparationJob, PreparationJobStatus
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash, _render
from ..security import require_session


def build_preparation_router(context: WebContext) -> APIRouter:
    """Build enqueue, progress, cancellation, and retry routes."""

    router = APIRouter()

    @router.post("/projects/{project_id}/summary/check")
    async def check_project_data(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        job = enqueue_preparation(context, project_id)
        request.session.pop("summary_error", None)
        _flash(request, "Preparation started. You can follow each step here.")
        return RedirectResponse(_progress_url(project_id, job.job_id), status_code=303)

    @router.get(
        "/projects/{project_id}/preparation/{job_id}",
        response_class=HTMLResponse,
    )
    async def preparation_progress(request: Request, project_id: str, job_id: str):
        require_session(request)
        return _render_progress(request, _get_job(context, project_id, job_id))

    @router.get("/projects/{project_id}/preparation/{job_id}/status")
    async def preparation_status(request: Request, project_id: str, job_id: str):
        require_session(request)
        return JSONResponse(_job_payload(_get_job(context, project_id, job_id)))

    @router.post("/projects/{project_id}/preparation/{job_id}/cancel")
    async def cancel_preparation(request: Request, project_id: str, job_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            _manager(context).cancel(project_id, job_id)
        except PreparationJobNotFoundError as error:
            raise HTTPException(status_code=404, detail="Preparation job not found") from error
        _flash(request, "Impodo will stop safely after the current source batch.")
        return RedirectResponse(_progress_url(project_id, job_id), status_code=303)

    @router.post("/projects/{project_id}/preparation/{job_id}/retry")
    async def retry_preparation(request: Request, project_id: str, job_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            project = context.queries.get(project_id)
            selection = context.queries.get_source_selection(project_id)
            total_rows = (
                sum(item.row_count for item in selection.datasets)
                if selection
                else 0
            )
            job = _manager(context).retry(
                project_id,
                job_id,
                project.name,
                total_rows,
                actor=context.actor,
            )
        except PreparationJobNotFoundError as error:
            raise HTTPException(status_code=404, detail="Preparation job not found") from error
        except PreparationJobStateError as error:
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

    project = context.queries.get(project_id)
    selection = context.queries.get_source_selection(project_id)
    total_rows = sum(item.row_count for item in selection.datasets) if selection else 0
    return _manager(context).enqueue(
        project_id,
        project.name,
        total_rows,
        actor=context.actor,
    )


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
        job=job,
        job_payload=_job_payload(job),
        failure_message=job.failure_message,
        error=error,
        status_code=status_code,
    )


def _job_payload(job: PreparationJob) -> dict[str, object]:
    redirect_url = ""
    if job.status is PreparationJobStatus.SUCCEEDED:
        redirect_url = f"/projects/{job.project_id}/normalization"
    elif job.status is PreparationJobStatus.REVIEW_REQUIRED:
        redirect_url = f"/projects/{job.project_id}/resolution"
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "phase": job.phase.value,
        "message": job.message,
        "completed_rows": job.completed_rows,
        "total_rows": job.total_rows,
        "progress_percent": job.progress_percent,
        "cancel_requested": job.cancel_requested,
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
    return f"/projects/{project_id}/preparation/{job_id}"
