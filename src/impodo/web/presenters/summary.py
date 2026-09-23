"""Summary web helpers."""

from __future__ import annotations

from time import perf_counter
from urllib.parse import urlencode

from fastapi import HTTPException, Request

from impodo.domain.errors import ReadinessError
from impodo.domain.preflight.deferred_scope import DeferredScopeEvidenceError
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.domain.compiler.browser_mapping_compiler import browser_mapping_labels
from impodo.domain.shared.access import AuthorizationError, Capability
from ...application.workspace.preparation.bounded_preparation import (
    supports_bounded_direct_preparation,
)
from ...application.odoo_connection_service import OdooConnectionPurpose
from ...application.odoo_read_failures import (
    OdooReadCredentialMissingError,
    OdooReadFailure,
    classify_odoo_read_failure,
)
from ...application.workspace.preparation.preparation_capability import (
    compile_preparation_capability,
)
from ...domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT,
    browser_evaluation_scale,
)
from impodo.adapters.odoo.local_stack import LocalStackError, LocalStackStatus
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode, SourceMode
from impodo.adapters.artifacts.reporting import WORKBOOK_NAME
from impodo.application.preflight_service import DEFERRED_SCOPE_DECISION_NAME
from ..constants import (
    DEFAULT_SUMMARY_ROWS_PER_PAGE,
    NORMALIZATION_GROUPS_PER_PAGE,
    ODOO_APPLICATIONS,
    SUMMARY_ROW_PAGE_SIZES,
)
from ..context import WebContext
from ..forms import _positive_query_int
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential_status,
)
from .common import _render
from .comparison_recovery import comparison_recovery_view
from .missing_parent_actions import missing_parent_source_key


def _render_target(
    request: Request,
    context: WebContext,
    workspace_state: WorkspaceState,
    *,
    error: str | None = None,
    status_code: int = 200,
    open_local_stack: bool = False,
    setup_attention_requested: bool = False,
):
    connection_purpose = (
        OdooConnectionPurpose.SOURCE_READ
        if workspace_state.source_mode is SourceMode.ODOO
        else OdooConnectionPurpose.TARGET_READ
    )
    return _render(
        request,
        "workspace_target.html",
        workspace_state=workspace_state,
        applications=ODOO_APPLICATIONS,
        local_stack=context.local_stack.get(workspace_state.workspace_id),
        remote_connection=context.remote_connections.get(
            workspace_state,
            connection_purpose,
        ),
        connection_purpose=connection_purpose,
        read_credential_status=get_target_credential_status(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.READ,
        ),
        write_credential_status=get_target_credential_status(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.WRITE,
        ),
        local_stack_auto_open=open_local_stack,
        local_stack_dialog_error=None,
        local_stack_support_error=None,
        local_stack_return_to="target",
        local_stack_resume_compare=False,
        local_stack_resume_ready=False,
        setup_attention_requested=setup_attention_requested,
        error=error,
        status_code=status_code,
    )


def _render_normalization(
    request: Request,
    context: WebContext,
    workspace_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    workspace_state = context.queries.get(workspace_id)
    review = context.normalization.current_group_review(workspace_id)
    if review is None:
        return _render_summary(
            request,
            context,
            workspace_id,
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
    requested_page = _positive_query_int(
        request.query_params.get("page"), default=1,
    )
    set_aside_page = None
    if selected_status == "set_aside":
        set_aside_page = context.queries.get_quality_review_page(
            workspace_id,
            summary.quality_run_id,
            status="quarantined",
            dataset="",
            page=requested_page,
            page_size=NORMALIZATION_GROUPS_PER_PAGE,
        )
    collision_groups = {}
    collision_routes = {}
    quality_field_labels: dict[str, dict[str, str]] = {}
    quality_issue_field_labels: dict[str, tuple[str, ...]] = {}
    quality_cause_groups = ()
    if set_aside_page is not None:
        selection = context.queries.get_mapping_source_selection(workspace_id)
        revision = context.queries.get_mapping_revision(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        quality_field_labels = _quality_field_label_map(
            revision,
            selection,
            schema,
        )
        quality_issue_field_labels = _quality_issue_field_label_map(
            set_aside_page.items,
            quality_field_labels,
        )
        quality_cause_groups = _quality_cause_groups(
            set_aside_page.items,
            quality_field_labels,
        )
        collision_items = tuple(
            item for item in set_aside_page.items
            if any(
                issue.reason_code == "POST_TRANSFORM_IDENTITY_COLLISION"
                for issue in item.issues
            )
        )
        if collision_items:
            collision_groups = context.queries.get_quality_collision_groups(
                workspace_id,
                summary.quality_run_id,
                tuple(item.row.row_id for item in collision_items),
            )
            if selection is not None:
                mapped = (
                    {item.dataset_id: item for item in revision.definition.datasets}
                    if revision is not None else {}
                )
                collision_routes = {
                    dataset.name: {
                        "index": index,
                        "rows_url": (
                            f"/workspaces/{workspace_id}/mapping"
                            f"?mapping_dataset={index}#rows-to-use-{index}"
                        ),
                        "match_url": (
                            f"/workspaces/{workspace_id}/mapping"
                            f"?mapping_dataset={index}#target-identity-{index}"
                        ),
                        "source_key_fields": tuple(
                            column.source_name
                            for column in dataset.columns
                            if column.stable_key in (
                                mapped[dataset.dataset_id].source_identity_column_keys
                                if dataset.dataset_id in mapped else ()
                            )
                        ),
                    }
                    for index, dataset in enumerate(selection.datasets)
                }
    page_count = max(
        1,
        (len(matching) + NORMALIZATION_GROUPS_PER_PAGE - 1)
        // NORMALIZATION_GROUPS_PER_PAGE,
    )
    page = min(
        requested_page,
        page_count,
    )
    start = (page - 1) * NORMALIZATION_GROUPS_PER_PAGE
    page_items = matching[start : start + NORMALIZATION_GROUPS_PER_PAGE]
    matching_count = len(matching)
    if set_aside_page is not None:
        page = set_aside_page.page
        page_count = set_aside_page.page_count
        matching_count = set_aside_page.matching_count
        page_items = ()
    return _render(
        request,
        "workspace_normalization.html",
        workspace_state=workspace_state,
        normalization=summary,
        dry_run=dry_run,
        review_items=page_items,
        set_aside_page=set_aside_page,
        quality_cause_groups=quality_cause_groups,
        quality_issue_field_labels=quality_issue_field_labels,
        collision_groups=collision_groups,
        collision_routes=collision_routes,
        set_aside_row_start=(page - 1) * NORMALIZATION_GROUPS_PER_PAGE + 1,
        set_aside_row_end=min(page * NORMALIZATION_GROUPS_PER_PAGE, matching_count),
        rejected_items=tuple(
            item for item in items if item["decision"] == "REJECTED"
        ),
        review_matching_count=matching_count,
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
    workspace_id: str,
    *,
    error: str | None = None,
    local_stack_error: str | None = None,
    local_stack_support_error: str | None = None,
    open_local_stack: bool = False,
    comparison_failure: OdooReadFailure | None = None,
    status_code: int = 200,
):
    summary_started = perf_counter()
    context_started = summary_started
    session_error = request.session.pop("summary_error", None)
    if error is None and isinstance(session_error, str):
        error = session_error
    navigation_started = perf_counter()
    navigation_snapshot = context.navigation.get_for_workspace(workspace_id)
    navigation_read_ms = (perf_counter() - navigation_started) * 1000
    workspace_state = navigation_snapshot.workspace_state
    credential_owner = context.target_credential_workspace(
        workspace_id,
        workspace_state=workspace_state,
    )
    read_credential_status = get_target_credential_status(
        context.secret_store,
        credential_owner,
        TargetCredentialRole.READ,
    )
    schema_catalog = (
        context.queries.get_odoo_schema_catalog(workspace_id)
        if workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
        else None
    )
    remote_read_credential_missing = (
        workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
        and schema_catalog is not None
        and not read_credential_status.available
    )
    local_stack = context.local_stack.get(workspace_id)
    local_stack_matches = _local_stack_matches_project(workspace_state, local_stack)
    local_odoo_recovery_needed = (
        workspace_state.source_mode is not SourceMode.ODOO
        and
        workspace_state.odoo_connection_mode is OdooConnectionMode.LOCAL
        and (
            not local_stack_matches
            or not local_stack.odoo_ready
            or not local_stack.metadata_ready
        )
    )
    source_selection = context.queries.get_source_selection(workspace_id)
    effective_selection = context.queries.get_mapping_source_selection(workspace_id)
    derived_plan = context.queries.get_derived_entity_plan(workspace_id)
    revision = context.queries.get_mapping_revision(workspace_id)
    bounded_direct = (
        source_selection is not None
        and effective_selection is not None
        and supports_bounded_direct_preparation(
            source_selection,
            effective_selection,
            derived_plan,
        )
    )
    scale_limit = MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
    if bounded_direct:
        scale_limit = BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
    if (
        source_selection is not None
        and effective_selection is not None
        and revision is not None
    ):
        capability = compile_preparation_capability(
            definition=revision.definition,
            physical_selection=source_selection,
            effective_selection=effective_selection,
            source_snapshots=(
                context.queries.get_current_source_snapshots(workspace_id)
            ),
            derived_plan=derived_plan,
            current_ruleset=context.quality.current_ruleset(workspace_id),
            reference_bundle=(
                context.resolution.current_reference_bundle(workspace_id)
            ),
        )
        scale_limit = capability.supported_rows
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
        context.queries.get_mapping_submission(workspace_id, revision.version)
        if revision is not None
        else None
    )
    summary_context_ms = (perf_counter() - context_started) * 1000
    evidence_started = perf_counter()
    staging = context.preflight.current_staging(workspace_id)
    quality = context.quality.current_summary(workspace_id)
    normalization = context.normalization.current_summary(workspace_id)
    resolution = context.resolution.current_summary(workspace_id)
    if (
        quality is not None
        and (staging is None or quality.staging_run_id != staging.run_id)
    ):
        quality = None
    summary_evidence_ms = (perf_counter() - evidence_started) * 1000
    readiness_started = perf_counter()
    report = context.preflight.current_report(workspace_id)
    deferred_scope = None
    deferred_scope_available = bool(
        report is not None
        and workspace_state.source_mode is not SourceMode.ODOO
        and report.attention_count
    )
    decision_exists = False
    if report is not None:
        try:
            decision_exists = context.artifacts.report_exists(
                workspace_id,
                report.run_id,
                DEFERRED_SCOPE_DECISION_NAME,
            )
        except ArtifactStoreError as scope_error:
            if error is None:
                error = str(scope_error)
    if decision_exists:
        try:
            deferred_scope = context.preflight.deferred_scope_review(workspace_id)
        except (
            ArtifactStoreError,
            DeferredScopeEvidenceError,
            ReadinessError,
        ) as scope_error:
            if error is None:
                error = str(scope_error)
    missing_parent_views = []
    missing_parent_evidence_error = ""
    if report is not None:
        try:
            missing_parent_groups = context.preflight.current_missing_parent_groups(
                workspace_id
            )
        except ReadinessError as error:
            missing_parent_groups = ()
            missing_parent_evidence_error = str(error)
        if missing_parent_groups:
            allow_missing_parent_draft = (
                context.workspace_views.get(
                    workspace_id, actor=context.actor
                ).migration_project.data_classification.value != "RESTRICTED"
            )
            mapped_datasets = {
                item.dataset_id: item for item in revision.definition.datasets
            } if revision else {}
            sources = tuple(effective_selection.datasets) if effective_selection else ()
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            field_labels = {
                (model.name, field.name): field.label
                for model in (schema.models if schema else ())
                for field in model.fields
            }
            for group in missing_parent_groups:
                located = next(
                    (
                        (index, dataset)
                        for index, dataset in enumerate(sources)
                        if dataset.name == group.dataset
                    ),
                    None,
                )
                source_name = group.key_field
                draft_url = ""
                if located is not None:
                    index, dataset = located
                    mapping = mapped_datasets.get(dataset.dataset_id)
                    source_key = (
                        missing_parent_source_key(mapping, group)
                        if mapping is not None else None
                    )
                    source_column = next(
                        (column for column in dataset.columns
                         if column.stable_key == source_key),
                        None,
                    )
                    if (
                        source_column is not None
                        and allow_missing_parent_draft
                        and not group.scope_fields
                        and group.stage3_list_supported
                    ):
                        source_name = source_column.source_name
                        draft_url = (
                            f"/workspaces/{workspace_id}/mapping?"
                            + urlencode({
                                "mapping_dataset": index,
                                "missing_parent_run": report.run_id,
                                "missing_parent_group": group.group_id,
                            })
                            + f"#rows-to-use-{index}"
                        )
                    elif source_column is not None:
                        source_name = source_column.source_name
                missing_parent_views.append({
                    "group": group,
                    "dataset_label": next(
                        (item.label for item in report.datasets
                         if item.dataset == group.dataset),
                        group.dataset,
                    ),
                    "field_label": field_labels.get(
                        (next((item.target_model for item in report.datasets
                               if item.dataset == group.dataset), ""), group.field),
                        group.field.replace("_", " ").title(),
                    ),
                    "source_name": source_name,
                    "draft_url": draft_url,
                })
    if (
        comparison_failure is None
        and remote_read_credential_missing
        and (
            report is not None
            or (normalization is not None and normalization.frozen)
        )
    ):
        comparison_failure = classify_odoo_read_failure(
            OdooReadCredentialMissingError(
                "The Odoo read key is not available for this target."
            )
        )
    comparison_recovery = (
        comparison_recovery_view(workspace_id, comparison_failure)
        if comparison_failure is not None
        else None
    )
    summary_readiness_ms = (perf_counter() - readiness_started) * 1000
    execution_started = perf_counter()
    load_preview = navigation_snapshot.facts.execution_preview
    deferred_scope_available = bool(
        deferred_scope_available
        or (
            report is not None
            and workspace_state.source_mode is not SourceMode.ODOO
            and load_preview is not None
            and load_preview.scope_error == "The reviewed load shape needs attention"
        )
    )
    summary_execution_ms = (perf_counter() - execution_started) * 1000
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
    quality_page_started = perf_counter()
    if quality is not None:
        quality_page = context.queries.get_quality_review_page(
            workspace_id,
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
    quality_field_labels = _quality_field_label_map(
        revision,
        effective_selection,
        schema_catalog,
    )
    quality_issue_field_labels = _quality_issue_field_label_map(
        quality_page.items if quality_page is not None else (),
        quality_field_labels,
    )
    quality_cause_groups = _quality_cause_groups(
        quality_page.items if quality_page is not None else (),
        quality_field_labels,
    )
    summary_quality_page_ms = (perf_counter() - quality_page_started) * 1000
    status_filter = request.query_params.get("status", "").strip()
    if status_filter not in {
        "",
        "ready",
        "create",
        "update",
        "unchanged",
        "attention",
        "needs_review",
        "blocked",
    }:
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
    readiness_page_started = perf_counter()
    if report is not None:
        persisted_page = context.preflight.readiness_rows(
            workspace_id,
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
    summary_readiness_ms += (perf_counter() - readiness_page_started) * 1000
    render_started = perf_counter()
    response = _render(
        request,
        "workspace_summary.html",
        workspace_state=workspace_state,
        revision=revision,
        submission=submission,
        staging=staging,
        quality=quality,
        normalization=normalization,
        resolution=resolution,
        quality_review_page=quality_page,
        quality_cause_groups=quality_cause_groups,
        quality_issue_field_labels=quality_issue_field_labels,
        quality_review_row_start=quality_row_start,
        quality_review_row_end=quality_row_end,
        quality_status=quality_status,
        quality_dataset=quality_dataset,
        quality_page_size=quality_page_size,
        quality_page_size_options=tuple(
            {
                "size": size,
                "url": _quality_summary_url(
                    workspace_id,
                    status=quality_status,
                    dataset=quality_dataset,
                    page_size=size,
                ),
            }
            for size in SUMMARY_ROW_PAGE_SIZES
        ),
        quality_previous_url=(
            _quality_summary_url(
                workspace_id,
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
                workspace_id,
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
        deferred_scope=deferred_scope,
        deferred_scope_available=deferred_scope_available,
        missing_parent_views=tuple(missing_parent_views),
        missing_parent_evidence_error=missing_parent_evidence_error,
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
                    workspace_id,
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
                workspace_id,
                page=row_page - 1 if row_page > 2 else None,
                page_size=readiness_page_size,
            )
            if row_page > 1
            else None
        ),
        readiness_row_next_url=(
            _summary_rows_url(
                request,
                workspace_id,
                page=row_page + 1,
                page_size=readiness_page_size,
            )
            if row_page < row_page_count
            else None
        ),
        review_workbook_ready=(
            report is not None
            and context.artifacts.report_exists(
                workspace_id, report.run_id, WORKBOOK_NAME
            )
        ),
        status_filter=status_filter,
        dataset_filter=dataset_filter,
        evaluation_scale=evaluation_scale,
        preparation_limit_message=preparation_limit_message,
        local_stack=local_stack,
        local_odoo_recovery_needed=local_odoo_recovery_needed,
        read_credential_status=read_credential_status,
        comparison_recovery=comparison_recovery,
        comparison_recovery_needed=comparison_recovery is not None,
        local_stack_auto_open=(
            open_local_stack
            or request.query_params.get("local_stack") == "1"
            or local_stack_error is not None
        ),
        local_stack_dialog_error=local_stack_error,
        local_stack_support_error=local_stack_support_error,
        local_stack_return_to="summary_compare",
        local_stack_resume_compare=True,
        local_stack_resume_ready=(
            local_stack_matches
            and local_stack.odoo_ready
            and local_stack.metadata_ready
        ),
        error=error,
        status_code=status_code,
        _workspace_navigation_facts=navigation_snapshot.facts,
        _navigation_read_ms=navigation_read_ms,
    )
    summary_render_ms = (perf_counter() - render_started) * 1000
    _append_summary_server_timing(
        response,
        summary_context=summary_context_ms,
        summary_evidence=summary_evidence_ms,
        summary_execution=summary_execution_ms,
        summary_quality_page=summary_quality_page_ms,
        summary_readiness=summary_readiness_ms,
        summary_render=summary_render_ms,
        total=(perf_counter() - summary_started) * 1000,
    )
    return response


def _quality_field_label_map(
    revision,
    selection,
    schema_catalog,
) -> dict[str, dict[str, str]]:
    """Return dataset-scoped labels for source, target, and synthetic fields."""

    labels: dict[str, dict[str, str]] = {}
    if revision is None or selection is None:
        return labels
    try:
        _dataset_labels, compiled = browser_mapping_labels(
            revision.definition,
            selection,
        )
    except (AttributeError, ReadinessError, TypeError):
        return labels
    for (dataset, field), label in compiled.items():
        labels.setdefault(dataset, {})[field] = label

    target_labels = {
        (model.name, field.name): field.label
        for model in (schema_catalog.models if schema_catalog is not None else ())
        for field in model.fields
    }
    datasets = {item.dataset_id: item for item in selection.datasets}
    for mapping in revision.definition.datasets:
        dataset = datasets.get(mapping.dataset_id)
        if dataset is None:
            continue
        scoped = labels.setdefault(dataset.name, {})
        target_fields = {
            item.target_field for item in mapping.fields
        } | {
            item.target_field for item in mapping.relationships
        } | {
            field
            for component in (*mapping.target_identity, *mapping.target_scope)
            for field in component.target_fields
        }
        for field in target_fields:
            scoped.setdefault(
                field,
                target_labels.get(
                    (mapping.target_model, field),
                    field.replace("_", " ").title(),
                ),
            )
        scoped.setdefault("Odoo match", "Odoo match")
        scoped.setdefault("Odoo match scope", "Odoo match scope")
    return labels


def _quality_issue_field_label_map(
    items,
    field_labels: dict[str, dict[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Resolve issue field identifiers once before rendering a review page."""

    return {
        issue.issue_id: tuple(
            field_labels.get(item.row.dataset, {}).get(
                field,
                field.replace("_", " ").title(),
            )
            for field in issue.affected_fields
        )
        for item in items
        for issue in item.issues
    }


def _quality_cause_groups(
    items,
    field_labels: dict[str, dict[str, str]],
) -> tuple[dict[str, object], ...]:
    """Group findings on the visible page into direct and inherited causes."""

    titles = {
        "SOURCE_TYPE_INVALID": "Invalid number or value type",
        "SOURCE_FORMULA_INVALID": "Formula calculation failed",
        "INCOMING_RELATIONSHIP_MISSING": "Linked source record is missing",
        "INCOMING_RELATIONSHIP_AMBIGUOUS": "Linked source record is ambiguous",
        "INCOMING_RELATIONSHIP_PARENT_SET_ASIDE": "Linked parent was set aside",
        "INCOMING_IDENTITY_DEPENDENT_SET_ASIDE": (
            "Dependent identity group was set aside"
        ),
    }
    inherited_reasons = {
        "INCOMING_RELATIONSHIP_PARENT_SET_ASIDE",
        "INCOMING_IDENTITY_DEPENDENT_SET_ASIDE",
    }
    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for item in items:
        scoped = field_labels.get(item.row.dataset, {})
        for issue in item.issues:
            fields = tuple(
                scoped.get(field, field.replace("_", " ").title())
                for field in issue.affected_fields
            )
            key = (issue.reason_code, issue.message, fields)
            group = grouped.setdefault(
                key,
                {
                    "reason_code": issue.reason_code,
                    "title": titles.get(
                        issue.reason_code,
                        issue.reason_code.replace("_", " ").title(),
                    ),
                    "kind": (
                        "Inherited dependency"
                        if issue.reason_code in inherited_reasons
                        else "Direct finding"
                    ),
                    "message": issue.message,
                    "fields": fields,
                    "count": 0,
                },
            )
            group["count"] = int(group["count"]) + 1
    return tuple(
        sorted(
            grouped.values(),
            key=lambda item: (
                -int(item["count"]),
                str(item["title"]),
                str(item["message"]),
            ),
        )
    )


def _append_summary_server_timing(response, **metrics: float) -> None:
    """Expose allowlisted phase durations to local request diagnostics."""

    timing = ", ".join(
        f"{name};dur={max(0.0, duration):.1f}"
        for name, duration in metrics.items()
    )
    existing = response.headers.get("Server-Timing", "")
    response.headers["Server-Timing"] = (
        f"{existing}, {timing}" if existing else timing
    )


def _local_stack_matches_project(
    workspace_state: WorkspaceState,
    status: LocalStackStatus,
) -> bool:
    """Return whether the selected local profile targets this exact project."""

    profile = status.profile
    if profile is None:
        return False
    if profile.base_url.rstrip("/") != workspace_state.odoo_base_url.rstrip("/"):
        return False
    return not (
        profile.database_hint
        and workspace_state.odoo_database
        and profile.database_hint != workspace_state.odoo_database
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
    workspace_id: str,
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
    base = f"/workspaces/{workspace_id}/summary"
    url = f"{base}?{query}" if query else base
    return f"{url}#readiness-rows"


def _quality_summary_url(
    workspace_id: str,
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
    base = f"/workspaces/{workspace_id}/summary"
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
    workspace_state: WorkspaceState,
) -> None:
    try:
        context.workspace_access.resolve(
            workspace_state.workspace_id,
            actor=context.actor,
            capability=Capability.LOCAL_STACK_INSPECT,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to inspect the local Odoo stack",
        ) from error
    if (
        workspace_state.odoo_connection_mode is not None
        and workspace_state.odoo_connection_mode is not OdooConnectionMode.LOCAL
    ):
        raise LocalStackError(
            "The local readiness assistant is available only in Local Odoo mode."
        )


def _require_local_stack_start(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> None:
    _require_local_stack_access(context, workspace_state)
    try:
        context.workspace_access.resolve(
            workspace_state.workspace_id,
            actor=context.actor,
            capability=Capability.LOCAL_STACK_START,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to start the local Odoo stack",
        ) from error


def _require_local_stack_stop(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> None:
    _require_local_stack_access(context, workspace_state)
    try:
        context.workspace_access.resolve(
            workspace_state.workspace_id,
            actor=context.actor,
            capability=Capability.LOCAL_STACK_STOP,
        )
    except AuthorizationError as error:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to stop the local Odoo stack",
        ) from error
