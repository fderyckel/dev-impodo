"""Expose read-only Odoo comparison and its review artifacts.

Migration stage: H — read-only target preflight. Layer: web route.

The compare action supplies
:class:`impodo.application.preflight_service.PreflightService` with a
project-bound read-only snapshot reader. Other routes download the already-
published technical manifest or build/download its human review workbook
projection. No route exposes an Odoo write operation.

See ``docs/architecture/python-code-map.md`` and
``tests/test_preflight_service.py``.
"""

from __future__ import annotations
from collections.abc import Iterator
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from ...artifacts import ArtifactStoreError
from ...connectors import ConnectorError
from ...projects import ProjectError
from ...domain.errors import ReadinessError
from ...application.preflight_service import MANIFEST_NAME
from ...reporting import (
    ReportGenerationError,
    WORKBOOK_NAME,
    write_review_workbook,
)
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _secure_form
from ..presenters.common import _flash
from ..presenters.summary import _render_summary
from ..target_readers import _read_readiness_snapshots


def _report_chunks(
    context: WebContext,
    project_id: str,
    run_id: str,
    filename: str,
) -> Iterator[bytes]:
    """Stream a protected report artifact without loading it all in memory."""

    with context.artifacts.materialize_report(
        project_id, run_id, filename
    ) as path:
        with path.open("rb") as report:
            while chunk := report.read(64 * 1024):
                yield chunk


def build_preflight_router(context: WebContext) -> APIRouter:
    """Build compare, manifest, and workbook routes for current Stage H evidence."""

    router = APIRouter()

    @router.post("/projects/{project_id}/summary/compare")
    async def compare_project_data(request: Request, project_id: str):
        """Compare the exact approved rows through a bounded read-only reader."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.queries.get(project_id)

        def reader(metadata_requests, record_requests):
            return _read_readiness_snapshots(
                context,
                project,
                metadata_requests,
                record_requests,
            )

        try:
            await run_in_threadpool(
                context.preflight.compare,
                project_id,
                reader=reader,
                actor=context.actor,
            )
        except (
            ConnectorError,
            ProjectError,
            ReadinessError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_summary(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Prepared data compared with Odoo. Nothing was changed.")
        return RedirectResponse(
            f"/projects/{project_id}/summary",
            status_code=303,
        )

    @router.get("/projects/{project_id}/summary/manifest")
    async def download_readiness_manifest(request: Request, project_id: str):
        require_session(request)
        report = context.preflight.current_report(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        try:
            exists = context.artifacts.report_exists(
                project_id, report.run_id, MANIFEST_NAME
            )
        except ArtifactStoreError as error:
            raise HTTPException(
                status_code=404, detail="Readiness manifest not found"
            ) from error
        if not exists:
            raise HTTPException(status_code=404, detail="Readiness manifest not found")
        filename = f"impodo-{project_id[:8]}-preflight.json"
        return StreamingResponse(
            _report_chunks(
                context, project_id, report.run_id, MANIFEST_NAME
            ),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/projects/{project_id}/summary/package")
    async def generate_readiness_package(request: Request, project_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        report = context.preflight.current_report(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        staging = context.preflight.current_staging(project_id)
        if staging is None or not staging.control_totals_passed:
            return _render_summary(
                request,
                context,
                project_id,
                error=(
                    "Resolve the named totals that need attention before "
                    "creating the package."
                ),
                status_code=422,
            )
        quality = context.quality.current_summary(project_id)
        if quality is None or quality.run_id != report.quality_run_id:
            return _render_summary(
                request,
                context,
                project_id,
                error="Check all rows again before creating the package.",
                status_code=422,
            )
        if not quality.ready_for_package:
            return _render_summary(
                request,
                context,
                project_id,
                error=(
                    "Resolve the data checks that need review or setup before "
                    "creating the package. Records already set aside may remain "
                    "outside the package."
                ),
                status_code=422,
            )
        if report.status != "READY":
            return _render_summary(
                request,
                context,
                project_id,
                error="Resolve the rows that need attention before creating the package.",
                status_code=422,
            )
        def write_package() -> None:
            with context.artifacts.materialize_report(
                project_id, report.run_id, MANIFEST_NAME
            ) as manifest_path:
                with context.artifacts.prepare_report(
                    project_id, report.run_id, WORKBOOK_NAME
                ) as workbook_path:
                    write_review_workbook(manifest_path, workbook_path)

        try:
            await run_in_threadpool(write_package)
        except (ArtifactStoreError, OSError, ReportGenerationError) as error:
            return _render_summary(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Review package created.")
        return RedirectResponse(
            f"/projects/{project_id}/summary",
            status_code=303,
        )

    @router.get("/projects/{project_id}/summary/workbook")
    async def download_readiness_workbook(request: Request, project_id: str):
        require_session(request)
        report = context.preflight.current_report(project_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        try:
            exists = context.artifacts.report_exists(
                project_id, report.run_id, WORKBOOK_NAME
            )
        except ArtifactStoreError as error:
            raise HTTPException(
                status_code=404, detail="Review package not found"
            ) from error
        if not exists:
            raise HTTPException(status_code=404, detail="Review package not found")
        filename = f"impodo-{project_id[:8]}-review.xlsx"
        return StreamingResponse(
            _report_chunks(
                context, project_id, report.run_id, WORKBOOK_NAME
            ),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router
