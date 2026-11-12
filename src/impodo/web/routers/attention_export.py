"""Download Stage 5 correction evidence even when rows cannot proceed.

The whole local read and Excel build runs on one worker. Authorization and
current bindings are checked before capture and again before returning bytes.
No artifact is published and no connection survives the response boundary.
"""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from impodo.adapters.artifacts.attention_sources import read_attention_sources
from impodo.adapters.artifacts.attention_workbook import build_attention_workbook
from impodo.adapters.artifacts.reporting import ReportGenerationError
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.application.workspace.access import bind_workspace_access_context
from impodo.application.workspace.review.attention_export import (
    verify_attention_export_current,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_page_read
from impodo.web.context import WebContext
from impodo.web.presenters.summary import _render_summary
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session


def build_attention_export_router(context: WebContext) -> APIRouter:
    """Keep the correction download independent of package and load readiness."""

    router = APIRouter()

    def download(request: Request, workspace_id: str, counts_only: bool) -> Response:
        actor = request_identity(request, context.identity_resolver).actor
        access = context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ
        )
        with bind_workspace_access_context(access):
            export = context.preflight.attention_export(
                workspace_id,
                actor=actor,
                expected_quality_run_id=request.query_params.get("quality", ""),
                expected_comparison_id=request.query_params.get("comparison", ""),
            )
            content = b""
            if not counts_only:
                sources = read_attention_sources(export, context.preflight.artifacts)
                content = build_attention_workbook(export, sources)
            # Resolution also checks parent lifecycle and current permissions.
            context.workspace_access.resolve(
                workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ
            )
            verify_attention_export_current(context.preflight, export, actor=actor)
        if counts_only:
            return JSONResponse(
                {
                    "affected_records": export.affected_record_count,
                    "findings": len(export.findings),
                    "problems": [
                        asdict(problem) for problem in export.problem_summaries
                    ],
                },
                headers={"Cache-Control": "no-store"},
            )
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="impodo-{workspace_id[:8]}-needs-attention.xlsx"',
                "Cache-Control": "no-store",
            },
        )

    async def respond(request: Request, workspace_id: str, *, counts_only: bool):
        require_session(request)
        try:
            return await run_page_read(download, request, workspace_id, counts_only)
        except AuthorizationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except (
            ArtifactStoreError,
            ReadinessError,
            ReportGenerationError,
            WorkspaceError,
            ValueError,
            OSError,
        ) as error:
            if counts_only:
                return JSONResponse(
                    {"detail": str(error)},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            return await run_page_read(
                _render_summary,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=409,
            )

    @router.get("/workspaces/{workspace_id}/summary/attention.xlsx")
    async def download_attention(request: Request, workspace_id: str):
        require_session(request)
        return await respond(request, workspace_id, counts_only=False)

    @router.get("/workspaces/{workspace_id}/summary/attention/counts")
    async def attention_counts(request: Request, workspace_id: str):
        require_session(request)
        return await respond(request, workspace_id, counts_only=True)

    return router
