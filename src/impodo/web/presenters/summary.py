"""Summary web helpers."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import HTTPException, Request

from ...access import AuthorizationError, Capability
from ...application.bounded_preparation import (
    direct_preparation_row_limit,
    supports_bounded_direct_preparation,
)
from ...domain.errors import ReadinessError
from ...domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    BROWSER_EVALUATION_ROW_LIMIT,
    browser_evaluation_scale,
)
from ...local_stack import LocalStackError
from ...projects import MigrationProject, OdooConnectionMode
from ...reporting import WORKBOOK_NAME
from ...workspace_errors import WorkspaceError
from ..constants import (
    DEFAULT_SUMMARY_ROWS_PER_PAGE,
    NORMALIZATION_GROUPS_PER_PAGE,
    ODOO_APPLICATIONS,
    SUMMARY_ROW_PAGE_SIZES,
)
from ..context import WebContext
from ..forms import _positive_query_int
from .common import _render


def _render_target(
    request: Request,
    context: WebContext,
    project: MigrationProject,
    *,
    error: str | None = None,
    status_code: int = 200,
    open_local_stack: bool = False,
):
    return _render(
        request,
        "project_target.html",
        project=project,
        applications=ODOO_APPLICATIONS,
        local_stack=context.local_stack.get(project.project_id),
        remote_connection=context.remote_connections.get(project),
        open_local_stack=open_local_stack,
        error=error,
        status_code=status_code,
    )


def _render_normalization(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    project = context.queries.get(project_id)
    review = context.normalization.current_group_review(project_id)
    if review is None:
        return _render_summary(
            request,
            context,
            project_id,
            error=(error or "Prepare the data before reviewing its changes."),
            status_code=(status_code if error else 422),
        )
    summary, groups, dry_run, automatic_record_count = review
    decisions = {item.key: item for item in dry_run.group_decisions}
    items = []
    for group in groups:
        recorded_decision = decisions.get(group.decision_key)
        decision = recorded_decision.decision.value if recorded_decision else ""
        if group.eligible_count == 0:
            item_status = "set_aside"
        elif not group.requires_decision:
            item_status = "automatic"
        elif decision:
            item_status = "reviewed"
        else:
            item_status = "pending"
        items.append(
            {
                "group": group,
                "status": item_status,
                "decision": decision,
                "reason": (
                    recorded_decision.evidence.reason
                    if recorded_decision is not None
                    else ""
                ),
            }
        )
    selected_status = request.query_params.get("status", "").strip()
    if selected_status not in {"", "automatic", "pending", "reviewed", "set_aside"}:
        selected_status = ""
    matching = tuple(
        item for item in items
        if not selected_status or item["status"] == selected_status
    )
    page_count = max(
        1,
        (len(matching) + NORMALIZATION_GROUPS_PER_PAGE - 1)
        // NORMALIZATION_GROUPS_PER_PAGE,
    )
    page = min(
        _positive_query_int(request.query_params.get("page"), default=1),
        page_count,
    )
    start = (page - 1) * NORMALIZATION_GROUPS_PER_PAGE
    page_items = matching[start : start + NORMALIZATION_GROUPS_PER_PAGE]
    return _render(
        request,
        "project_normalization.html",
        project=project,
        normalization=summary,
        dry_run=dry_run,
        review_items=page_items,
        rejected_items=tuple(
            item for item in items if item["decision"] == "REJECTED"
        ),
        review_matching_count=len(matching),
        review_status=selected_status,
        review_page=page,
        review_page_count=page_count,
        review_previous_url=(
            f"?{urlencode({'status': selected_status, 'page': page - 1})}"
            if page > 1
            else None
        ),
        review_next_url=(
            f"?{urlencode({'status': selected_status, 'page': page + 1})}"
            if page < page_count
            else None
        ),
        automatic_record_count=automatic_record_count,
        error=error,
        status_code=status_code,
    )


def _render_summary(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    session_error = request.session.pop("summary_error", None)
    if error is None and isinstance(session_error, str):
        error = session_error
    project = context.queries.get(project_id)
    source_selection = context.queries.get_source_selection(project_id)
    effective_selection = context.queries.get_mapping_source_selection(project_id)
    derived_plan = context.queries.get_derived_entity_plan(project_id)
    revision = context.queries.get_mapping_revision(project_id)
    bounded_direct = (
        source_selection is not None
        and effective_selection is not None
        and supports_bounded_direct_preparation(
            source_selection,
            effective_selection,
            derived_plan,
        )
    )
    scale_limit = BROWSER_EVALUATION_ROW_LIMIT
    if bounded_direct:
        scale_limit = BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
        if revision is not None and effective_selection is not None:
            scale_limit = direct_preparation_row_limit(
                revision.definition,
                effective_selection,
                context.queries.get_current_source_snapshots(project_id),
            )
    evaluation_scale = (
        browser_evaluation_scale(
            source_selection,
            supported_limit=scale_limit,
        )
        if source_selection is not None
        else None
    )
    preparation_limit_message = (
        _preparation_limit_message(
            bounded_direct=bounded_direct,
            supported_limit=scale_limit,
        )
        if evaluation_scale is not None and not evaluation_scale.supported
        else ""
    )
    submission = (
        context.queries.get_mapping_submission(project_id, revision.version)
        if revision is not None
        else None
    )
    staging = context.preflight.current_staging(project_id)
    quality = context.quality.current_summary(project_id)
    normalization = context.normalization.current_summary(project_id)
    resolution = context.resolution.current_summary(project_id)
    if (
        quality is not None
        and (staging is None or quality.staging_run_id != staging.run_id)
    ):
        quality = None
    report = context.preflight.current_report(project_id)
    try:
        load_preview = context.execution.current_preview(project_id)
    except (ReadinessError, WorkspaceError):
        # Historical or manually repaired preflight evidence may predate the
        # execution artifact. It can still be reviewed and compared again.
        load_preview = None
    quality_status = request.query_params.get("quality_status", "").strip()
    if quality_status not in {"", "ready", "review", "quarantined", "blocked"}:
        quality_status = ""
    quality_dataset = request.query_params.get("quality_dataset", "").strip()
    quality_datasets = {
        item.dataset for item in (staging.datasets if staging else ())
    }
    if quality_dataset not in quality_datasets:
        quality_dataset = ""
    quality_page = None
    quality_row_start = 0
    quality_row_end = 0
    quality_page_size = _summary_page_size(
        request.query_params.get("quality_page_size")
    )
    if quality is not None:
        quality_page = context.queries.get_quality_review_page(
            project_id,
            quality.run_id,
            status=quality_status,
            dataset=quality_dataset,
            page=_positive_query_int(
                request.query_params.get("quality_page"),
                default=1,
            ),
            page_size=quality_page_size,
        )
        if quality_page.matching_count:
            quality_row_start = (
                (quality_page.page - 1) * quality_page_size
                + 1
            )
            quality_row_end = min(
                quality_page.page * quality_page_size,
                quality_page.matching_count,
            )
    status_filter = request.query_params.get("status", "").strip()
    if status_filter not in {"", "ready", "needs_review", "blocked"}:
        status_filter = ""
    dataset_filter = request.query_params.get("dataset", "").strip()
    available_datasets = {
        item.dataset for item in (report.datasets if report else ())
    }
    if dataset_filter not in available_datasets:
        dataset_filter = ""
    requested_row_page = _positive_query_int(
        request.query_params.get("page"),
        default=1,
    )
    readiness_page_size = _summary_page_size(
        request.query_params.get("page_size")
    )
    if report is not None:
        persisted_page = context.preflight.readiness_rows(
            project_id,
            report.run_id,
            status=status_filter,
            dataset=dataset_filter,
            page=requested_row_page,
            page_size=readiness_page_size,
        )
        rows = persisted_page.items
        row_total = persisted_page.matching_count
        row_page = persisted_page.page
        row_page_count = persisted_page.page_count
        row_start_index = (row_page - 1) * readiness_page_size
    else:
        rows = ()
        row_total = 0
        row_page = 1
        row_page_count = 1
        row_start_index = 0
    return _render(
        request,
        "project_summary.html",
        project=project,
        revision=revision,
        submission=submission,
        staging=staging,
        quality=quality,
        normalization=normalization,
        resolution=resolution,
        quality_review_page=quality_page,
        quality_review_row_start=quality_row_start,
        quality_review_row_end=quality_row_end,
        quality_status=quality_status,
        quality_dataset=quality_dataset,
        quality_page_size=quality_page_size,
        quality_page_size_options=tuple(
            {
                "size": size,
                "url": _quality_summary_url(
                    project_id,
                    status=quality_status,
                    dataset=quality_dataset,
                    page_size=size,
                ),
            }
            for size in SUMMARY_ROW_PAGE_SIZES
        ),
        quality_previous_url=(
            _quality_summary_url(
                project_id,
                status=quality_status,
                dataset=quality_dataset,
                page=quality_page.page - 1,
                page_size=quality_page_size,
            )
            if quality_page is not None and quality_page.page > 1
            else None
        ),
        quality_next_url=(
            _quality_summary_url(
                project_id,
                status=quality_status,
                dataset=quality_dataset,
                page=quality_page.page + 1,
                page_size=quality_page_size,
            )
            if quality_page is not None
            and quality_page.page < quality_page.page_count
            else None
        ),
        readiness=report,
        load_preview=load_preview,
        readiness_rows=rows,
        readiness_row_total=row_total,
        readiness_row_start=(row_start_index + 1 if row_total else 0),
        readiness_row_end=min(
            row_start_index + readiness_page_size,
            row_total,
        ),
        readiness_page_size=readiness_page_size,
        readiness_page_size_options=tuple(
            {
                "size": size,
                "url": _summary_rows_url(
                    request,
                    project_id,
                    page=None,
                    page_size=size,
                ),
            }
            for size in SUMMARY_ROW_PAGE_SIZES
        ),
        readiness_row_page=row_page,
        readiness_row_page_count=row_page_count,
        readiness_row_previous_url=(
            _summary_rows_url(
                request,
                project_id,
                page=row_page - 1 if row_page > 2 else None,
                page_size=readiness_page_size,
            )
            if row_page > 1
            else None
        ),
        readiness_row_next_url=(
            _summary_rows_url(
                request,
                project_id,
                page=row_page + 1,
                page_size=readiness_page_size,
            )
            if row_page < row_page_count
            else None
        ),
        review_workbook_ready=(
            report is not None
            and context.artifacts.report_exists(
                project_id, report.run_id, WORKBOOK_NAME
            )
        ),
        status_filter=status_filter,
        dataset_filter=dataset_filter,
        evaluation_scale=evaluation_scale,
        preparation_limit_message=preparation_limit_message,
        error=error,
        status_code=status_code,
    )


def _preparation_limit_message(
    *,
    bounded_direct: bool,
    supported_limit: int,
) -> str:
    """Explain a preparation size boundary without exposing its backend."""

    if bounded_direct:
        return (
            "With this source setup and these field rules, Impodo can safely "
            f"prepare up to {supported_limit:,} rows in one project."
        )
    return (
        "This setup includes related or grouped source data, so Impodo can "
        f"safely prepare up to {supported_limit:,} rows in one project."
    )


def _summary_rows_url(
    request: Request,
    project_id: str,
    *,
    page: int | None,
    page_size: int,
) -> str:
    params = {
        name: value
        for name, value in request.query_params.items()
        if name in {"status", "dataset"} and len(value) <= 256
    }
    if page is not None and page > 1:
        params["page"] = str(page)
    if page_size != DEFAULT_SUMMARY_ROWS_PER_PAGE:
        params["page_size"] = str(page_size)
    query = urlencode(params)
    base = f"/projects/{project_id}/summary"
    url = f"{base}?{query}" if query else base
    return f"{url}#readiness-rows"


def _quality_summary_url(
    project_id: str,
    *,
    status: str = "",
    dataset: str = "",
    page: int | None = None,
    page_size: int = DEFAULT_SUMMARY_ROWS_PER_PAGE,
) -> str:
    params = {}
    if status:
        params["quality_status"] = status
    if dataset:
        params["quality_dataset"] = dataset
    if page is not None and page > 1:
        params["quality_page"] = str(page)
    if page_size != DEFAULT_SUMMARY_ROWS_PER_PAGE:
        params["quality_page_size"] = str(page_size)
    query = urlencode(params)
    base = f"/projects/{project_id}/summary"
    url = f"{base}?{query}" if query else base
    return f"{url}#quality-rows"


def _summary_page_size(value: str | None) -> int:
    """Return one bounded summary-table page size."""

    try:
        page_size = int(value or DEFAULT_SUMMARY_ROWS_PER_PAGE)
    except ValueError:
        return DEFAULT_SUMMARY_ROWS_PER_PAGE
    return (
        page_size
        if page_size in SUMMARY_ROW_PAGE_SIZES
        else DEFAULT_SUMMARY_ROWS_PER_PAGE
    )


def _require_local_stack_access(
    context: WebContext,
    project: MigrationProject,
) -> None:
    try:
        context.authorization.require(
            context.actor,
            Capability.LOCAL_STACK_INSPECT,
            project_id=project.project_id,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to inspect the local Odoo stack",
        ) from error
    if (
        project.odoo_connection_mode is not None
        and project.odoo_connection_mode is not OdooConnectionMode.LOCAL
    ):
        raise LocalStackError(
            "The local readiness assistant is available only in Local Odoo mode."
        )


def _require_local_stack_start(
    context: WebContext,
    project: MigrationProject,
) -> None:
    _require_local_stack_access(context, project)
    try:
        context.authorization.require(
            context.actor,
            Capability.LOCAL_STACK_START,
            project_id=project.project_id,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to start the local Odoo stack",
        ) from error


def _require_local_stack_stop(
    context: WebContext,
    project: MigrationProject,
) -> None:
    _require_local_stack_access(context, project)
    try:
        context.authorization.require(
            context.actor,
            Capability.LOCAL_STACK_STOP,
            project_id=project.project_id,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to stop the local Odoo stack",
        ) from error
