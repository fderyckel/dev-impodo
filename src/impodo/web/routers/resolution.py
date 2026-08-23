"""Possible-duplicate review routes for Slice 6 effective data."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...domain.errors import ReadinessError
from ...workspace_state import WorkspaceStateError
from ...workspace_errors import WorkspaceError
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash, _render
from ..security import require_session
from .preparation import enqueue_preparation


def build_resolution_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    def render(
        request: Request,
        project_id: str,
        *,
        error: str | None = None,
        status_code: int = 200,
    ):
        project = context.queries.get(project_id)
        review = context.resolution.current_review(project_id)
        if review is None:
            return RedirectResponse(
                f"/workspaces/{project_id}/summary",
                status_code=303,
            )
        pending_pairs = sum(item.decision is None for item in review.candidates)
        pending_fields = sum(item.decision is None for item in review.fields)
        return _render(
            request,
            "workspace_resolution.html",
            project=project,
            review=review,
            pending_pairs=pending_pairs,
            pending_fields=pending_fields,
            error=error,
            status_code=status_code,
        )

    @router.get("/workspaces/{project_id}/resolution", response_class=HTMLResponse)
    async def review_duplicates(request: Request, project_id: str):
        require_session(request)
        return render(request, project_id)

    async def decide_pair(
        request: Request,
        project_id: str,
        candidate_id: str,
        *,
        same_record: bool,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version", "reason"},
        )
        try:
            await run_in_threadpool(
                context.resolution.decide_pair,
                project_id,
                str(form["run_id"]),
                candidate_id,
                same_record=same_record,
                expected_lifecycle_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
                reason=str(form["reason"]),
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return render(request, project_id, error=str(error), status_code=422)
        _flash(
            request,
            "Records marked as one business record."
            if same_record
            else "Records confirmed as separate.",
        )
        return RedirectResponse(f"/workspaces/{project_id}/resolution", status_code=303)

    @router.post("/workspaces/{project_id}/resolution/candidates/{candidate_id}/merge")
    async def merge_pair(request: Request, project_id: str, candidate_id: str):
        return await decide_pair(
            request,
            project_id,
            candidate_id,
            same_record=True,
        )

    @router.post("/workspaces/{project_id}/resolution/candidates/{candidate_id}/separate")
    async def separate_pair(request: Request, project_id: str, candidate_id: str):
        return await decide_pair(
            request,
            project_id,
            candidate_id,
            same_record=False,
        )

    @router.post("/workspaces/{project_id}/resolution/groups/{group_id}/fields/{field}/select")
    async def select_survivor(
        request: Request,
        project_id: str,
        group_id: str,
        field: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "run_id",
                "lifecycle_version",
                "row_ids",
                "selected_row_id",
                "reason",
            },
        )
        try:
            await run_in_threadpool(
                context.resolution.select_survivor_field,
                project_id,
                str(form["run_id"]),
                group_id,
                field,
                tuple(sorted(str(form["row_ids"]).split(","))),
                selected_row_id=str(form["selected_row_id"]),
                expected_lifecycle_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
                reason=str(form["reason"]),
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return render(request, project_id, error=str(error), status_code=422)
        _flash(request, "Surviving field value selected.")
        return RedirectResponse(f"/workspaces/{project_id}/resolution", status_code=303)

    @router.post("/workspaces/{project_id}/resolution/groups/{group_id}/fields/{field}/correct")
    async def correct_survivor(
        request: Request,
        project_id: str,
        group_id: str,
        field: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "run_id",
                "lifecycle_version",
                "row_ids",
                "replacement_value",
                "reason",
            },
        )
        try:
            await run_in_threadpool(
                context.resolution.correct_survivor_field,
                project_id,
                str(form["run_id"]),
                group_id,
                field,
                tuple(sorted(str(form["row_ids"]).split(","))),
                replacement_text=str(form["replacement_value"]),
                expected_lifecycle_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
                reason=str(form["reason"]),
            )
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return render(request, project_id, error=str(error), status_code=422)
        _flash(request, "Corrected survivor value recorded for this review.")
        return RedirectResponse(f"/workspaces/{project_id}/resolution", status_code=303)

    @router.post("/workspaces/{project_id}/resolution/approve")
    async def approve_resolution(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "run_id", "lifecycle_version"},
        )
        try:
            await run_in_threadpool(
                context.resolution.approve,
                project_id,
                str(form["run_id"]),
                expected_lifecycle_version=int(str(form["lifecycle_version"])),
                actor=context.actor,
            )
            job = enqueue_preparation(context, project_id)
        except (WorkspaceStateError, ReadinessError, WorkspaceError, ValueError) as error:
            return render(request, project_id, error=str(error), status_code=422)
        _flash(request, "Duplicate review approved. Preparation is continuing.")
        return RedirectResponse(
            f"/workspaces/{project_id}/preparation/{job.job_id}",
            status_code=303,
        )

    return router

