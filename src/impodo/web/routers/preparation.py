"""Translate the browser preparation action into the Stages E–G use case.

Layer: web route. The router delegates to
``WebContext.preparation.prepare`` in a worker thread, presents expected
workflow failures on the Summary page, and redirects successful preparation to
normalization review. It performs no row evaluation or persistence itself.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

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
    """Build the route that prepares frozen data for explicit review."""

    router = APIRouter()

    @router.post("/projects/{project_id}/summary/check")
    async def check_project_data(request: Request, project_id: str):
        """Run target-independent preparation and open normalization review."""

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
            try:
                resolution = context.resolution.current_review(project_id)
            except (ReadinessError, WorkspaceError):
                resolution = None
            if (
                resolution is not None
                and resolution.summary.status == "REVIEW_REQUIRED"
                and resolution.candidates
            ):
                request.session.pop("summary_error", None)
                _flash(request, "Review the possible duplicate records before continuing.")
                return RedirectResponse(
                    f"/projects/{project_id}/resolution",
                    status_code=303,
                )
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
