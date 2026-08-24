"""Expose Project CutoverPlan qualification and rollout selection."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...migration_foundation import MigrationFoundationError
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session


def build_cutover_plans_router(context: WebContext) -> APIRouter:
    """Build the explicit review, qualification, and selection routes."""

    router = APIRouter()

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/qualification",
        response_class=HTMLResponse,
    )
    async def integrated_qualification(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        require_session(request)
        return _render_qualification(
            request,
            context,
            project_id,
            migration_run_id,
        )

    @router.post(
        "/projects/{project_id}/runs/{migration_run_id}/qualification"
    )
    async def qualify_integrated_test(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "operation_id",
                "expected_workspace_revision",
                "expected_evidence_hash",
            },
        )
        try:
            context.cutover_plans.qualify(
                project_id,
                migration_run_id,
                expected_workspace_revision=int(
                    _text(form, "expected_workspace_revision")
                ),
                expected_evidence_hash=_text(form, "expected_evidence_hash"),
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (MigrationFoundationError, TypeError, ValueError) as error:
            return _render_qualification(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "The exact integrated Test evidence is now qualified.")
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}/qualification",
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/runs/{migration_run_id}/qualification/select"
    )
    async def select_integrated_qualification(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "operation_id",
                "expected_workspace_revision",
                "qualification_id",
            },
        )
        try:
            context.cutover_plans.select(
                project_id,
                _text(form, "qualification_id"),
                expected_workspace_revision=int(
                    _text(form, "expected_workspace_revision")
                ),
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (MigrationFoundationError, TypeError, ValueError) as error:
            return _render_qualification(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Selected this qualification as the Project rollout candidate.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}/qualification",
            status_code=303,
        )

    return router


def _render_qualification(
    request: Request,
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    project = context.migration_projects.get(project_id, actor=context.actor)
    review = context.cutover_plans.review(
        project_id,
        migration_run_id,
        actor=context.actor,
    )
    current = context.migration_projects.get(project_id, actor=context.actor)
    recipes = {
        item.recipe_id: item
        for item in context.recipes.list(project_id, actor=context.actor)
    }
    evidence = {item.application_id: item for item in review.evidence}
    issues = {
        application.workspace_id: tuple(
            item
            for item in review.issues
            if item.workspace_id == application.workspace_id
        )
        for application in review.applications
    }
    project_issues = tuple(
        item for item in review.issues if item.workspace_id is None
    )
    return _render(
        request,
        "project_integrated_qualification.html",
        project=project,
        review=review,
        recipes=recipes,
        evidence=evidence,
        issues=issues,
        project_issues=project_issues,
        expected_workspace_revision=current.optimistic_revision,
        operation_id=str(uuid4()),
        error=error,
        status_code=status_code,
    )
