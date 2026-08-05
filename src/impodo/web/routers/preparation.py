"""Preparation browser routes."""

from __future__ import annotations
from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...connectors import ConnectorError
from ...projects import ProjectError
from ...domain.errors import ReadinessError
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash


def build_preparation_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.post("/projects/{project_id}/summary/check")
    async def check_project_data(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            await run_in_threadpool(
                context.preparation.prepare,
                project_id,
                actor=context.actor,
            )
        except (
            ConnectorError,
            ProjectError,
            ReadinessError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            request.session["summary_error"] = str(error)
            return RedirectResponse(
                f"/projects/{project_id}/summary",
                status_code=303,
            )
        request.session.pop("summary_error", None)
        _flash(request, "Prepared data is ready for your review.")
        return RedirectResponse(
            f"/projects/{project_id}/normalization",
            status_code=303,
        )

    return router
