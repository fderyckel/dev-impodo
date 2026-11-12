"""Present ordinary review and load stages for an exact-target continuation.

The page uses the existing correction comparison and execution owners. It
contains no target matching or Odoo writer. All reads are authorized and local;
comparison and loading run through the existing background-job boundaries.
"""

from io import BytesIO
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from starlette.concurrency import run_in_threadpool

from impodo.domain.correction import CorrectionPlan
from impodo.domain.shared.access import Capability

from ..composition.page_reads import run_local_operation, run_page_read
from ..context import WebContext
from ..presenters.common import _render
from ..request_identity import request_identity
from ..security import require_session


def build_continuation_router(context: WebContext) -> APIRouter:
    """Expose the same staged review for a working revision of a saved load."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/continuation/workbook")
    async def continuation_workbook(request: Request, workspace_id: str):
        require_session(request)

        def capture():
            actor = request_identity(request, context.identity_resolver).actor
            context.workspace_access.resolve(
                workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ
            )
            binding = context.corrections.binding_for_successor(workspace_id)
            if binding is None:
                raise HTTPException(404, "Saved comparison not found")
            plan = context.corrections.current_plan(binding.completed_workspace_id)
            if (
                plan is None
                or request.query_params.get("comparison_id") != plan.plan_id
            ):
                raise HTTPException(
                    409, "Choose this load's saved comparison before downloading"
                )
            return plan

        plan = await run_local_operation(capture)
        payload = await run_in_threadpool(_workbook_bytes, plan)
        return Response(
            payload,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": 'attachment; filename="saved-load-review.xlsx"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/workspaces/{workspace_id}/continuation/{step}")
    async def continuation_page(request: Request, workspace_id: str, step: str):
        require_session(request)
        if step not in {"prepared", "compare", "results", "confirm", "outcome"}:
            raise HTTPException(404, "Continuation step not found")
        return await run_page_read(
            _render_continuation, request, context, workspace_id, step
        )

    return router


def _render_continuation(
    request: Request, context: WebContext, workspace_id: str, step: str
):
    """Capture a bounded protected review without contacting Odoo or mutating it."""

    actor = request_identity(request, context.identity_resolver).actor
    context.workspace_access.resolve(
        workspace_id, actor=actor, capability=Capability.PROTECTED_EVIDENCE_READ
    )
    binding = context.corrections.binding_for_successor(workspace_id)
    if binding is None:
        raise HTTPException(404, "This workspace does not continue a completed load")
    view = context.corrections.get(binding.completed_workspace_id, actor=actor)
    if view is None:
        raise HTTPException(404, "The saved load is unavailable")
    active = context.correction_jobs.active(binding.completed_workspace_id)
    if active is not None and step in {"compare", "confirm"}:
        return RedirectResponse(
            f"/workspaces/{binding.completed_workspace_id}/correction/progress/{active.job_id}?continue=1",
            303,
        )
    state = context.queries.get(workspace_id)
    quality = context.quality.current_summary(workspace_id)
    normalization = context.normalization.current_summary(workspace_id)
    plan = context.corrections.current_plan(binding.completed_workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    labels = (
        {
            (model.name, field.name): field.label
            for model in schema.models
            for field in model.fields
        }
        if schema
        else {}
    )
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    fields = (*plan.fields, *plan.observations) if plan is not None else ()
    page_count = max(1, (len(fields) + 49) // 50)
    page = min(page, page_count)
    rows = tuple(
        {
            "dataset": field.dataset,
            "row": field.source_row,
            "field": labels.get(
                (field.target_model, field.target_field), field.target_field
            ),
            "kind": field.value_kind.value,
            "current": field.current,
            "proposed": field.corrected,
            "reference": field.review_value,
            "outcome": field.outcome or "READY",
        }
        for field in fields[(page - 1) * 50 : page * 50]
    )
    return _render(
        request,
        "workspace_continuation.html",
        workspace_state=state,
        correction_binding=binding,
        continuation_view=view,
        continuation_step=step,
        current_plan=plan,
        comparison_rows=rows,
        page=page,
        page_count=page_count,
        quality=quality,
        normalization=normalization,
        can_compare=bool(
            quality
            and quality.ready_for_package
            and normalization
            and normalization.frozen
        ),
        latest_job=context.correction_jobs.latest(binding.completed_workspace_id),
        review_request_id=str(uuid4()),
        confirmation_id=str(uuid4()),
    )


def _workbook_bytes(plan: CorrectionPlan) -> bytes:
    """Build a text-safe export after releasing local database owners."""

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Reviewed changes")
    sheet.append(
        (
            "Table",
            "Source row",
            "Odoo model",
            "Field",
            "Current value",
            "Reviewed value",
            "Result",
        )
    )
    for field in (*plan.fields, *plan.observations):
        values = (
            field.dataset,
            field.source_row,
            field.target_model,
            field.target_field,
            field.current
            if field.value_kind.value == "SCALAR"
            else "Existing linked record",
            field.corrected
            if field.value_kind.value == "SCALAR"
            else field.review_value,
            field.outcome or "Update",
        )
        cells = []
        for value in values:
            cell = WriteOnlyCell(
                sheet,
                value=value
                if isinstance(value, (int, float, bool))
                else str(value if value is not None else "Empty"),
            )
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
