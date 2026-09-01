"""Expose the Stage D mapping editor and its evidence transitions.

Layer: web route. The save action parses the browser form into complete
dataset-centric contracts, first preserves a recoverable working draft, then
optionally asks ``MappingWorkspaceService`` to validate an immutable revision
or submit it. Preview and transformation-impact routes are projections and do
not authorize later migration stages.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_mapping_workflow.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
import csv
import hashlib
import json
from dataclasses import replace
from io import StringIO
from time import perf_counter
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from impodo.adapters.artifacts.mapping_review import (
    MappingReviewGenerationError,
    mapping_review_workbook_name,
    write_mapping_review_workbook,
)
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.run.contracts import (
    MigrationRunPlanningError,
    RecipeApplicationStatus,
)
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.contracts import MappingWorkingDraft
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateError
from impodo.web.composition.target_readers import (
    _refresh_mapping_odoo_defaults,
    _relationship_value_choices,
    _source_value_choices,
)

from ...application.odoo_read_failures import classify_odoo_read_failure
from ...domain.errors import ReadinessError
from ...domain.mapping.contracts import (
    MAPPING_CONTRACT_VERSION,
    SUPPORTED_MAPPING_CONTRACT_VERSIONS,
    TargetFieldHandling,
    UnsupportedMappingContractError,
)
from ...domain.mapping.validation.evidence import MappingValidationResult
from ...domain.staging.transformation_impact import TransformationImpactFilter
from ..constants import (
    TRANSFORMATION_IMPACT_OUTCOMES,
    TRANSFORMATION_IMPACT_PAGE_SIZE,
)
from ..context import WebContext
from ..diagnostics import set_diagnostic_working_draft_version
from ..forms import (
    _is_json_request,
    _mapping_request_form,
    _optional_int,
    _optional_nonnegative_query_int,
    _secure_form,
    _text,
    _texts,
)
from ...domain.mapping.mutations import (
    MappingMutationAction,
    MappingMutationReceipt,
    MappingMutationState,
    MappingVersionConflict,
)
from ..mapping_formula_authoring import (
    formula_allowed_names,
    mapping_formula_authoring_issues,
    saved_with_formula_issues_message,
    validate_formula_authoring,
)
from ..mapping_catalog_runtime import (
    MappingCatalogCapacityError,
    MappingCatalogProjectionCache,
    MappingCatalogSearchCoordinator,
)
from ..presenters.common import _flash, _render
from ..presenters.mapping_forms import (
    _active_mapping_definition,
    _mapping_allowed_fields,
    _mapping_datasets_from_form,
    _merge_partial_mapping_datasets,
    _related_business_keys,
)
from ..presenters.mapping_impact import (
    _mapping_return_url,
    _mapping_save_error_response,
    _transformation_impact_evidence,
    _transformation_impact_filters,
    _transformation_impact_identity,
    _transformation_impact_labels,
    _transformation_impact_row_views,
    _transformation_impact_url,
    _transformation_rule_impact_views,
)
from ..presenters.mapping_view import (
    _render_mapping,
    _render_mapping_field_catalog,
    _safe_spreadsheet_text,
)
from ..security import require_csrf, require_session


def build_mapping_router(context: WebContext) -> APIRouter:
    """Build mapping editor, preview, impact, validation, and submission routes."""

    router = APIRouter()
    catalog_projection_cache = MappingCatalogProjectionCache(maximum_entries=64)
    catalog_searches = MappingCatalogSearchCoordinator(maximum_editors=256)

    @router.get("/workspaces/{workspace_id}/mapping", response_class=HTMLResponse)
    async def workspace_mapping(request: Request, workspace_id: str):
        require_session(request)
        active_url = _active_preparation_url(context, workspace_id)
        if active_url:
            return RedirectResponse(active_url, status_code=303)
        queued_at = perf_counter()

        def render_mapping_page():
            queue_wait_ms = (perf_counter() - queued_at) * 1000
            try:
                response = _render_mapping(request, context, workspace_id)
            except UnsupportedMappingContractError as error:
                response = _render(
                    request,
                    "mapping/unsupported.html",
                    workspace_state=context.queries.get(workspace_id),
                    workspace_navigation=None,
                    saved_mapping_contract_version=error.contract_version,
                    current_mapping_contract_version=MAPPING_CONTRACT_VERSION,
                    supported_mapping_contract_versions=tuple(
                        sorted(SUPPORTED_MAPPING_CONTRACT_VERSIONS)
                    ),
                    status_code=409,
                )
            _append_server_timing(response, "queue_wait", queue_wait_ms)
            return response

        return await run_in_threadpool(render_mapping_page)

    @router.get(
        "/workspaces/{workspace_id}/mapping/field-catalog",
        response_class=HTMLResponse,
    )
    async def workspace_mapping_field_catalog(
        request: Request,
        workspace_id: str,
    ):
        """Return only the saved scalar-field catalogue for browser search."""

        require_session(request)
        _require_mapping_idle(context, workspace_id)
        search_generation = _mapping_catalog_search_generation(request)
        queued_at = perf_counter()

        def render_catalog():
            queue_wait_ms = (perf_counter() - queued_at) * 1000
            response = _render_mapping_field_catalog(
                request,
                context,
                workspace_id,
                catalog_projection_cache,
            )
            _append_server_timing(response, "queue_wait", queue_wait_ms)
            return response

        if search_generation is None:
            return await run_in_threadpool(render_catalog)
        editor_id, generation = search_generation
        catalog_kind = (
            "relation"
            if request.query_params.get("catalog") == "relation"
            else "scalar"
        )

        async def render_latest_catalog():
            return await run_in_threadpool(render_catalog)

        try:
            response = await catalog_searches.run_latest(
                (
                    workspace_id,
                    context.actor.identity.issuer,
                    context.actor.identity.subject_id,
                    editor_id,
                    catalog_kind,
                ),
                generation,
                render_latest_catalog,
                work_key=(
                    workspace_id,
                    context.actor.identity.issuer,
                    context.actor.identity.subject_id,
                ),
            )
        except MappingCatalogCapacityError:
            return Response(
                "Field search is busy. Wait a moment and try again.",
                status_code=503,
                headers={
                    "Cache-Control": "no-store",
                    "Retry-After": "1",
                },
            )
        if response is None:
            return Response(
                status_code=204,
                headers={
                    "Cache-Control": "no-store",
                    "X-Impodo-Catalog-Generation": str(generation),
                    "X-Impodo-Catalog-Result": "superseded",
                },
            )
        response.headers["X-Impodo-Catalog-Generation"] = str(generation)
        response.headers["X-Impodo-Catalog-Result"] = "current"
        return response

    @router.post("/workspaces/{workspace_id}/mapping/value-choices")
    async def workspace_mapping_value_choices(
        request: Request,
        workspace_id: str,
    ):
        """Return bounded source and Odoo choices for the mapping dialog."""

        require_session(request)
        _require_mapping_idle(context, workspace_id)
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "kind",
                "dataset_id",
                "source_column_key",
                "target_model",
                "target_field",
                "business_key_id",
                "refresh",
            },
        )
        try:
            kind = _text(form, "kind")
            if kind not in {"scalar", "relationship"}:
                raise WorkspaceError("Choose a supported Odoo field")
            selection = context.queries.get_mapping_source_selection(
                workspace_id
            )
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            governance = context.queries.get_schema_governance(workspace_id)
            if selection is None or schema is None:
                raise WorkspaceError(
                    "Freeze the source and confirm the Odoo schema first"
                )
            dataset_id = _text(form, "dataset_id")
            source_column_key = _text(form, "source_column_key")
            source_dataset = next(
                (
                    item
                    for item in selection.datasets
                    if item.dataset_id == dataset_id
                ),
                None,
            )
            if source_dataset is None or source_column_key not in {
                item.stable_key for item in source_dataset.columns
            }:
                raise WorkspaceError("Choose one current source column")
            source_choices = await run_in_threadpool(
                _source_value_choices,
                context,
                workspace_id,
                dataset_id,
                source_column_key,
            )
            target_model = _text(form, "target_model")
            target_field = _text(form, "target_field")
            model = next(
                (item for item in schema.models if item.name == target_model),
                None,
            )
            field = next(
                (
                    item
                    for item in (model.fields if model else ())
                    if item.name == target_field
                ),
                None,
            )
            if model is None or field is None:
                raise WorkspaceError("Choose one current Odoo field")
            ambiguous_values: tuple[str, ...] = ()
            target_checked_at: str | None = None
            target_choices_reused = False
            if kind == "scalar":
                if field.type != "selection":
                    raise WorkspaceError(
                        "This Odoo field is not a choice field"
                    )
                if not field.selection:
                    raise WorkspaceError(
                        "This Odoo field currently has no available choices; "
                        "refresh the Odoo fields before matching values"
                    )
                target_choices = tuple(
                    {
                        "value": str(value),
                        "label": str(label),
                    }
                    for value, label in field.selection
                )
            else:
                if governance is None:
                    raise WorkspaceError(
                        "Linked record matching requires confirmed Odoo matching rules"
                    )
                if field.type != "many2one" or not field.relation:
                    raise WorkspaceError(
                        "Value matching currently supports linked single records"
                    )
                key = next(
                    (
                        item
                        for item in _related_business_keys(
                            governance.business_keys,
                            field.relation,
                        )
                        if item.key_id == _text(form, "business_key_id")
                    ),
                    None,
                )
                if key is None:
                    raise WorkspaceError(
                        "Choose how the related Odoo record is identified first"
                    )
                workspace_state = context.queries.get(workspace_id)
                (
                    target_choices,
                    ambiguous_values,
                    checked_at,
                    target_choices_reused,
                ) = await run_in_threadpool(
                    _relationship_value_choices,
                    context,
                    workspace_state,
                    schema,
                    target_model,
                    field,
                    key,
                    refresh=_text(form, "refresh") == "1",
                )
                target_checked_at = checked_at.isoformat()
            return JSONResponse(
                {
                    "source_choices": source_choices,
                    "target_choices": target_choices,
                    "ambiguous_values": ambiguous_values,
                    "target_checked_at": target_checked_at,
                    "target_choices_reused": target_choices_reused,
                }
            )
        except (
            ArtifactStoreError,
            ConnectorError,
            WorkspaceStateError,
            SecretStoreError,
            SourceLoadError,
            WorkspaceError,
        ) as error:
            read_failure = classify_odoo_read_failure(error)
            return JSONResponse(
                {
                    "detail": str(error),
                    "read_credential_required": (
                        read_failure.asks_for_read_credential
                    ),
                    "read_credential_failure_code": (
                        read_failure.code.value
                        if read_failure.asks_for_read_credential
                        else ""
                    ),
                },
                status_code=422,
            )

    @router.post("/workspaces/{workspace_id}/mapping/formula-validation")
    async def workspace_mapping_formula_validation(
        request: Request,
        workspace_id: str,
    ):
        """Return one safe-parser authoring result without changing evidence."""

        require_session(request)
        _require_mapping_idle(context, workspace_id)
        form = await _mapping_request_form(request)
        _secure_form(request, form, {"csrf_token", "dataset_id", "formula"})
        selection = context.queries.get_mapping_source_selection(workspace_id)
        if selection is None:
            raise HTTPException(status_code=422, detail="Source selection missing")
        dataset_id = _text(form, "dataset_id")
        source_dataset = next(
            (
                dataset
                for dataset in selection.datasets
                if dataset.dataset_id == dataset_id
            ),
            None,
        )
        if source_dataset is None:
            raise HTTPException(
                status_code=422,
                detail="Choose one current source dataset",
            )
        issue = validate_formula_authoring(
            _text(form, "formula"),
            allowed_names=formula_allowed_names(source_dataset),
        )
        response = JSONResponse(
            {
                "valid": issue is None,
                "issue": issue.portable_dict() if issue is not None else None,
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/workspaces/{workspace_id}/mapping/transformation-impact",
        response_class=HTMLResponse,
    )
    async def workspace_transformation_impact(request: Request, workspace_id: str):
        """Render one bounded page from prepared transformation evidence."""

        require_session(request)
        active_url = _active_preparation_url(context, workspace_id)
        if active_url:
            return RedirectResponse(active_url, status_code=303)
        try:
            evidence = _transformation_impact_evidence(context, workspace_id)
        except WorkspaceError as error:
            return await run_in_threadpool(
                _render_mapping,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        workspace_state, revision, physical_selection, effective_selection, plan = evidence
        identity = _transformation_impact_identity(
            revision,
            physical_selection,
            effective_selection,
            plan,
        )
        snapshot = context.queries.get_transformation_impact_snapshot(
            workspace_id,
            identity,
        )
        (
            datasets,
            field_labels,
            field_choices,
            selection_labels,
        ) = _transformation_impact_labels(
            context, workspace_id, revision, effective_selection
        )
        filters = _transformation_impact_filters(
            dataset=request.query_params.get("dataset", ""),
            outcome=request.query_params.get("outcome", ""),
            target_field=request.query_params.get("field", ""),
            query=request.query_params.get("q", ""),
            allowed_datasets={item[0] for item in datasets},
            allowed_fields={item[0] for item in field_choices},
        )
        page = None
        previous_url = None
        next_url = None
        rows = ()
        outcome_urls = {
            outcome: _transformation_impact_url(
                workspace_id,
                replace(filters, outcome=outcome),
            )
            for outcome in TRANSFORMATION_IMPACT_OUTCOMES
        }
        if snapshot is not None:
            after = _optional_nonnegative_query_int(
                request.query_params.get("after")
            )
            before = (
                None
                if after is not None
                else _optional_nonnegative_query_int(
                    request.query_params.get("before")
                )
            )
            page = context.queries.get_transformation_impact_page(
                workspace_id,
                identity,
                filters,
                page_size=TRANSFORMATION_IMPACT_PAGE_SIZE,
                after=after,
                before=before,
            )
            rows = _transformation_impact_row_views(page.rows)
            if page.previous_before is not None:
                previous_url = _transformation_impact_url(
                    workspace_id,
                    filters,
                    before=page.previous_before,
                )
            if page.next_after is not None:
                next_url = _transformation_impact_url(
                    workspace_id,
                    filters,
                    after=page.next_after,
                )
        return _render(
            request,
            "workspace_transformation_impact.html",
            workspace_state=workspace_state,
            revision=revision,
            snapshot=snapshot,
            report=snapshot.report if snapshot is not None else None,
            rule_impact_views=_transformation_rule_impact_views(
                request,
                workspace_id,
                snapshot,
                revision,
                effective_selection,
                field_labels,
                selection_labels,
            ),
            rows=rows,
            datasets=datasets,
            field_labels=field_labels,
            field_choices=field_choices,
            filters=filters,
            impact_page=page,
            outcome_urls=outcome_urls,
            previous_url=previous_url,
            next_url=next_url,
            error=None,
        )

    @router.post(
        "/workspaces/{workspace_id}/mapping/transformation-impact/prepare"
    )
    async def prepare_transformation_impact(request: Request, workspace_id: str):
        """Prepare one hash-bound local snapshot without contacting Odoo."""

        require_session(request)
        _require_mapping_idle(context, workspace_id)
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            evidence = _transformation_impact_evidence(context, workspace_id)
        except WorkspaceError as error:
            return await run_in_threadpool(
                _render_mapping,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        workspace_state, revision, _physical_selection, effective_selection, _plan = evidence

        try:
            await run_in_threadpool(
                context.transformation_impacts.prepare_snapshot,
                workspace_id,
                actor=context.actor,
            )
        except (OSError, ReadinessError, WorkspaceError) as error:
            (
                datasets,
                field_labels,
                field_choices,
                _selection_labels,
            ) = _transformation_impact_labels(
                context, workspace_id, revision, effective_selection
            )
            return _render(
                request,
                "workspace_transformation_impact.html",
                workspace_state=workspace_state,
                revision=revision,
                snapshot=None,
                report=None,
                rule_impact_views=(),
                rows=(),
                datasets=datasets,
                field_labels=field_labels,
                field_choices=field_choices,
                filters=TransformationImpactFilter(),
                impact_page=None,
                outcome_urls={},
                previous_url=None,
                next_url=None,
                error=str(error),
                status_code=422,
            )
        _flash(request, "The changed-value comparison is ready.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/mapping/transformation-impact",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/mapping/transformation-impact.csv")
    async def download_transformation_impact(request: Request, workspace_id: str):
        """Download matching persisted impact rows without recomputing them."""

        require_session(request)
        _require_mapping_idle(context, workspace_id)
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "dataset", "outcome", "field", "q"},
        )
        try:
            evidence = _transformation_impact_evidence(context, workspace_id)
        except WorkspaceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        _project, revision, physical_selection, effective_selection, plan = evidence
        identity = _transformation_impact_identity(
            revision,
            physical_selection,
            effective_selection,
            plan,
        )
        (
            datasets,
            _field_labels,
            field_choices,
            _selection_labels,
        ) = _transformation_impact_labels(
            context, workspace_id, revision, effective_selection
        )
        filters = _transformation_impact_filters(
            dataset=_text(form, "dataset"),
            outcome=_text(form, "outcome"),
            target_field=_text(form, "field"),
            query=_text(form, "q"),
            allowed_datasets={item[0] for item in datasets},
            allowed_fields={item[0] for item in field_choices},
        )
        if context.queries.get_transformation_impact_snapshot(
            workspace_id,
            identity,
        ) is None:
            raise HTTPException(
                status_code=422,
                detail="Prepare the current transformation impact first",
            )

        hash_token = revision.definition.content_hash.removeprefix("sha256:")[:16]
        filter_token = hashlib.sha256(
            json.dumps(
                {
                    "dataset": filters.dataset,
                    "outcome": filters.outcome,
                    "field": filters.target_field,
                    "q": filters.query,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        scope = "filtered" if any(
            (
                filters.dataset,
                filters.outcome,
                filters.target_field,
                filters.query,
            )
        ) else "all"
        filename = (
            f"transformation-impact-{scope}-v{revision.version}-"
            f"{hash_token}-{filter_token}.csv"
        )

        def csv_chunks():
            stream = StringIO(newline="")
            writer = csv.writer(stream)
            stream.write("\ufeff")
            writer.writerow(
                (
                    "Dataset",
                    "Excel row",
                    "Source column",
                    "Odoo target field",
                    "Raw source",
                    "Proposed value",
                    "Rules applied",
                    "Result",
                    "Message",
                )
            )
            yield stream.getvalue()
            stream.seek(0)
            stream.truncate(0)
            for index, row in enumerate(
                context.queries.iter_transformation_impact_rows(
                    workspace_id,
                    identity,
                    filters,
                ),
                start=1,
            ):
                writer.writerow(
                    tuple(
                        _safe_spreadsheet_text(value)
                        for value in (
                            row.dataset,
                            row.source_row,
                            row.source_column,
                            row.target_field,
                            row.raw_value,
                            row.proposed_value,
                            row.rules,
                            row.outcome,
                            row.message,
                        )
                    )
                )
                if index % 1_000 == 0:
                    yield stream.getvalue()
                    stream.seek(0)
                    stream.truncate(0)
            if stream.tell():
                yield stream.getvalue()

        return StreamingResponse(
            csv_chunks(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @router.get(
        "/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}"
    )
    async def mapping_mutation_receipt(
        request: Request,
        workspace_id: str,
        operation_id: str,
    ):
        """Resolve a timed-out browser mutation without repeating its write."""

        require_session(request)
        try:
            receipt = await run_in_threadpool(
                context.mapping_workspace.get_mutation_receipt,
                workspace_id,
                operation_id,
                actor=context.actor,
            )
        except WorkspaceError:
            receipt = None
        if receipt is None:
            return JSONResponse(
                {
                    "operation_id": operation_id,
                    "status": "not_found",
                    "failure_code": "MAPPING_OPERATION_NOT_FOUND",
                    "message": (
                        "This operation did not reach Impodo. Nothing was saved."
                    ),
                }
            )
        return JSONResponse(
            _mapping_mutation_receipt_payload(
                request,
                workspace_id,
                receipt,
            )
        )

    @router.post("/workspaces/{workspace_id}/mapping/save")
    async def save_workspace_mapping(request: Request, workspace_id: str):
        """Run one explicit save, check, or confirmation command."""

        require_session(request)
        json_request = _is_json_request(request)
        if json_request:
            require_csrf(request, request.headers.get("x-csrf-token", ""))
        active_url = _active_preparation_url(context, workspace_id)
        if active_url:
            message = (
                "Preparation is using this workspace's saved data. Wait for it "
                "to finish before changing field matches."
            )
            if json_request:
                return JSONResponse(
                    {"detail": message, "redirect_url": active_url},
                    status_code=409,
                )
            _flash(request, message)
            return RedirectResponse(active_url, status_code=303)
        selection = context.queries.get_mapping_source_selection(workspace_id)
        if selection is None:
            raise HTTPException(status_code=422, detail="Source selection missing")
        requested_dataset = _optional_nonnegative_query_int(
            request.query_params.get("mapping_dataset")
        )
        active_dataset = min(
            requested_dataset if requested_dataset is not None else 0,
            max(len(selection.datasets) - 1, 0),
        )
        mapping_return_url = (
            f"{_mapping_return_url(request, workspace_id)}"
            f"#mapping-dataset-{active_dataset}"
        )

        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        governance = context.queries.get_schema_governance(workspace_id)
        if schema is None:
            raise HTTPException(status_code=422, detail="Odoo schema missing")
        operation_id = ""
        mutation_started = False
        try:
            form = await _mapping_request_form(request)
            allowed = _mapping_allowed_fields(form, selection, schema)
            _secure_form(request, form, allowed)
            action = _text(form, "action")
            if action not in {
                "save_progress",
                "draft",
                "submit",
                "remove_readonly",
                "confirm_defaults",
                "refresh_defaults",
            } and not action.startswith(
                ("set_disposition:", "clear_disposition:")
            ):
                raise WorkspaceError("Choose a supported matching action")
            expected_parent = _optional_int(
                _text(form, "expected_parent_version")
            )
            expected_working_version = _optional_int(
                _text(form, "expected_working_draft_version")
            )
            operation_id = _text(form, "operation_id") or str(uuid4())
            receipt = await run_in_threadpool(
                context.mapping_workspace.begin_mutation,
                workspace_id,
                operation_id=operation_id,
                action=_mapping_mutation_action(action),
                request_hash=_mapping_mutation_request_hash(form),
                submitted_working_draft_version=expected_working_version,
                submitted_mapping_revision_version=expected_parent,
                actor=context.actor,
            )
            mutation_started = True
            if receipt.replayed:
                payload = _mapping_mutation_receipt_payload(
                    request,
                    workspace_id,
                    receipt,
                )
                if json_request:
                    status_code = (
                        200
                        if receipt.state is MappingMutationState.COMMITTED
                        else (
                            409
                            if receipt.state is MappingMutationState.REJECTED
                            else 202
                        )
                    )
                    return JSONResponse(payload, status_code=status_code)
                _flash(request, str(payload["message"]))
                return RedirectResponse(mapping_return_url, status_code=303)
            if action.startswith(
                ("set_disposition:", "clear_disposition:")
            ):
                parts = action.split(":")
                expected_parts = 4 if parts[0] == "set_disposition" else 3
                if len(parts) != expected_parts:
                    raise WorkspaceError(
                        "The Odoo-required field decision is invalid"
                    )
                dataset_index = _optional_int(parts[1])
                if (
                    dataset_index is None
                    or dataset_index < 0
                    or dataset_index >= len(selection.datasets)
                ):
                    raise WorkspaceError(
                        "The matching table is no longer current"
                    )
                handling = (
                    TargetFieldHandling(parts[3])
                    if parts[0] == "set_disposition"
                    else None
                )
                draft = await run_in_threadpool(
                    context.mapping_workspace.set_target_field_disposition,
                    workspace_id,
                    dataset_id=selection.datasets[dataset_index].dataset_id,
                    target_field=parts[2],
                    handling=handling,
                    expected_version=expected_working_version,
                    actor=context.actor,
                    operation_id=operation_id,
                )
                decision_base_url = (
                    _mapping_return_url(
                        request,
                        workspace_id,
                        mapping_dataset=dataset_index,
                    )
                )
                if handling is TargetFieldHandling.ODOO_DEFAULT:
                    draft, validation = await _check_saved_default_decision(
                        context,
                        workspace_id,
                        draft,
                        expected_parent_version=expected_parent,
                        operation_id=operation_id,
                    )
                    message = _checked_default_message(
                        "Odoo will choose this value.",
                        validation,
                    )
                    decision_return_url = _checked_mapping_return_url(
                        request,
                        workspace_id,
                        validation,
                        success_url=(
                            f"{decision_base_url}"
                            f"#mapping-dataset-{dataset_index}"
                        ),
                    )
                else:
                    message = (
                        "Saved the Odoo-required field decision. "
                        "Check matches again when ready."
                        if handling is not None
                        else "Cleared the Odoo-required field decision."
                    )
                    decision_return_url = (
                        f"{decision_base_url}#next-step-blockers"
                    )
                if json_request:
                    return JSONResponse(
                        await _mapping_mutation_result_payload(
                            context,
                            request,
                            workspace_id,
                            operation_id,
                            message=message,
                            redirect_url=decision_return_url,
                        )
                    )
                _flash(request, message)
                return RedirectResponse(decision_return_url, status_code=303)
            if action == "remove_readonly":
                working_draft, removed_count = await run_in_threadpool(
                    context.mapping_workspace.remove_readonly_field_mappings,
                    workspace_id,
                    expected_version=expected_working_version,
                    actor=context.actor,
                    operation_id=operation_id,
                )
                message = (
                    f"Removed {removed_count} Odoo-managed field "
                    f"match{'es' if removed_count != 1 else ''}. "
                    "Check matches again when ready."
                )
                if json_request:
                    return JSONResponse(
                        await _mapping_mutation_result_payload(
                            context,
                            request,
                            workspace_id,
                            operation_id,
                            message=message,
                            redirect_url=mapping_return_url,
                        )
                    )
                _flash(request, message)
                return RedirectResponse(mapping_return_url, status_code=303)
            if action == "confirm_defaults":
                working_draft, confirmed_count = await run_in_threadpool(
                    context.mapping_workspace.confirm_available_odoo_defaults,
                    workspace_id,
                    expected_version=expected_working_version,
                    actor=context.actor,
                    operation_id=operation_id,
                )
                working_draft, validation = await _check_saved_default_decision(
                    context,
                    workspace_id,
                    working_draft,
                    expected_parent_version=expected_parent,
                    operation_id=operation_id,
                )
                message = _checked_default_message(
                    f"Confirmed {confirmed_count} Odoo default"
                    f"{'s' if confirmed_count != 1 else ''}.",
                    validation,
                )
                decision_return_url = _checked_mapping_return_url(
                    request,
                    workspace_id,
                    validation,
                    success_url=mapping_return_url,
                )
                if json_request:
                    return JSONResponse(
                        await _mapping_mutation_result_payload(
                            context,
                            request,
                            workspace_id,
                            operation_id,
                            message=message,
                            redirect_url=decision_return_url,
                        )
                    )
                _flash(request, message)
                return RedirectResponse(decision_return_url, status_code=303)
            if action == "refresh_defaults":
                requested_fields = await run_in_threadpool(
                    context.mapping_workspace.default_recovery_fields,
                    workspace_id,
                    actor=context.actor,
                )
                workspace_state = context.queries.get(workspace_id)
                refreshed_schema = await run_in_threadpool(
                    _refresh_mapping_odoo_defaults,
                    context,
                    workspace_state,
                    schema,
                    requested_fields,
                )
                if refreshed_schema.pending_refresh is not None:
                    change_count = refreshed_schema.pending_refresh.change_count
                    message = (
                        "Odoo changed since these fields were checked. Review "
                        f"{change_count} Odoo change"
                        f"{'s' if change_count != 1 else ''}; your checked "
                        "matches are preserved."
                    )
                    schema_review_url = (
                        f"/workspaces/{workspace_id}/schema#odoo-details"
                    )
                    await run_in_threadpool(
                        context.mapping_workspace.complete_mutation,
                        workspace_id,
                        operation_id,
                        actor=context.actor,
                        content_identity=refreshed_schema.content_hash,
                    )
                    _flash(request, message)
                    if json_request:
                        return JSONResponse(
                            await _mapping_mutation_result_payload(
                                context,
                                request,
                                workspace_id,
                                operation_id,
                                message=message,
                                redirect_url=schema_review_url,
                            )
                        )
                    return RedirectResponse(schema_review_url, status_code=303)
                working_draft, confirmed_count = await run_in_threadpool(
                    context.mapping_workspace.confirm_available_odoo_defaults,
                    workspace_id,
                    expected_version=expected_working_version,
                    actor=context.actor,
                    operation_id=operation_id,
                )
                working_draft, validation = await _check_saved_default_decision(
                    context,
                    workspace_id,
                    working_draft,
                    expected_parent_version=expected_parent,
                    operation_id=operation_id,
                )
                message = _checked_default_message(
                    f"Odoo will decide {confirmed_count} required field"
                    f"{'s' if confirmed_count != 1 else ''} using the defaults "
                    "checked for this target.",
                    validation,
                )
                decision_return_url = _checked_mapping_return_url(
                    request,
                    workspace_id,
                    validation,
                    success_url=mapping_return_url,
                )
                if json_request:
                    return JSONResponse(
                        await _mapping_mutation_result_payload(
                            context,
                            request,
                            workspace_id,
                            operation_id,
                            message=message,
                            redirect_url=decision_return_url,
                        )
                    )
                _flash(request, message)
                return RedirectResponse(decision_return_url, status_code=303)
            active_definition = _active_mapping_definition(
                context,
                workspace_id,
                selection,
                schema,
                governance,
            )
            datasets = _mapping_datasets_from_form(
                form,
                selection,
                schema,
                governance,
            )
            datasets = _merge_partial_mapping_datasets(
                datasets,
                active_definition,
                form,
                selection,
                schema,
            )
            if action == "save_progress":
                working_draft = await run_in_threadpool(
                    context.mapping_workspace.save_working_draft,
                    workspace_id,
                    datasets=datasets,
                    expected_version=expected_working_version,
                    actor=context.actor,
                    operation_id=operation_id,
                )
                set_diagnostic_working_draft_version(
                    request,
                    working_draft.version,
                )
                formula_issues = mapping_formula_authoring_issues(
                    working_draft.definition.datasets,
                    selection,
                )
                save_message = (
                    saved_with_formula_issues_message(len(formula_issues))
                    if formula_issues
                    else "Progress saved. Check matches when ready."
                )
                if json_request:
                    return JSONResponse(
                        await _mapping_mutation_result_payload(
                            context,
                            request,
                            workspace_id,
                            operation_id,
                            message=save_message,
                            redirect_url=mapping_return_url,
                            extra={
                                "saved_at": working_draft.updated_at.isoformat(),
                                "authoring_issues": [
                                    issue.portable_dict()
                                    for issue in formula_issues
                                ],
                            },
                        )
                    )
                _flash(
                    request,
                    save_message,
                )
                return RedirectResponse(
                    mapping_return_url,
                    status_code=303,
                )
            if action == "draft":
                _revision, validation = await run_in_threadpool(
                    context.mapping_workspace.check_definition,
                    workspace_id,
                    datasets=datasets,
                    expected_parent_version=expected_parent,
                    expected_working_draft_version=(
                        expected_working_version
                    ),
                    actor=context.actor,
                    operation_id=operation_id,
                )
                if validation.status.value == "INVALID":
                    message = "Matches checked. Review the items that need attention."
                    mapping_return_url = (
                        f"{_mapping_return_url(request, workspace_id)}"
                        "#next-step-blockers"
                    )
                else:
                    message = "Matches checked and ready to confirm."
                _flash(request, message)
            else:
                await run_in_threadpool(
                    context.mapping_workspace.submit_current,
                    workspace_id,
                    datasets=datasets,
                    expected_version=expected_parent,
                    expected_working_draft_version=(
                        expected_working_version
                    ),
                    warning_acknowledgements=_texts(
                        form, "warning_acknowledgement"
                    ),
                    actor=context.actor,
                    operation_id=operation_id,
                )
                access = context.workspace_access.require(
                    context.actor,
                    Capability.MAPPING_SUBMIT,
                    workspace_id=workspace_id,
                )
                if access.recipe_application_id is not None:
                    confirmed_application = await run_in_threadpool(
                        context.run_planning.confirm_application_mapping,
                        access.recipe_application_id,
                        actor=context.actor,
                    )
                    mapping_return_url = (
                        _confirmed_recipe_mapping_destination(
                            confirmed_application
                        )
                    )
                    if (
                        confirmed_application.status
                        is RecipeApplicationStatus.BLOCKED
                    ):
                        message = (
                            "Field matches confirmed. Continue with the remaining "
                            "Recipe review."
                        )
                    else:
                        message = "Field matches confirmed."
                else:
                    message = "Field matches confirmed."
                    mapping_return_url = f"/workspaces/{workspace_id}/prepare"
                _flash(request, message)
        except MappingVersionConflict as error:
            rejected = await _reject_mapping_mutation(
                context,
                workspace_id,
                operation_id,
                mutation_started=mutation_started,
                failure_code=error.code,
                failure_detail=str(error),
            )
            if (
                json_request
                and rejected is not None
                and rejected.state is MappingMutationState.COMMITTED
            ):
                payload = _mapping_mutation_receipt_payload(
                    request,
                    workspace_id,
                    rejected,
                )
                payload.update(
                    {
                        "message": (
                            "The Odoo-field decision was saved, but checking "
                            "met a newer mapping version. Reload to continue "
                            "from the saved state."
                        ),
                        "follow_up_status": "conflict",
                    }
                )
                return JSONResponse(payload)
            if json_request:
                payload = (
                    _mapping_mutation_receipt_payload(
                        request,
                        workspace_id,
                        rejected,
                    )
                    if rejected is not None
                    else {
                        "operation_id": operation_id,
                        "status": "rejected",
                        "failure_code": error.code,
                    }
                )
                payload.update(
                    {
                        "detail": str(error),
                        "submitted_working_draft_version": (
                            error.submitted_working_draft_version
                        ),
                        "submitted_mapping_revision_version": (
                            error.submitted_mapping_revision_version
                        ),
                        "current_working_draft_version": (
                            error.current_working_draft_version
                        ),
                        "current_mapping_revision_version": (
                            error.current_mapping_revision_version
                        ),
                        "recovery": {
                            "reload_url": _mapping_return_url(
                                request,
                                workspace_id,
                            ),
                            "copy_edits": True,
                        },
                    }
                )
                return JSONResponse(payload, status_code=409)
            request.session["mapping_error"] = str(error)
            return RedirectResponse(mapping_return_url, status_code=303)
        except HTTPException as error:
            rejected = await _reject_mapping_mutation(
                context,
                workspace_id,
                operation_id,
                mutation_started=mutation_started,
                failure_code=f"HTTP_{error.status_code}",
                failure_detail=str(error.detail),
            )
            if json_request and rejected is not None:
                payload = _mapping_mutation_receipt_payload(
                    request,
                    workspace_id,
                    rejected,
                )
                payload["detail"] = str(error.detail)
                return JSONResponse(payload, status_code=error.status_code)
            return _mapping_save_error_response(
                request,
                workspace_id,
                error,
                json_request=json_request,
            )
        except (
            ConnectorError,
            SecretStoreError,
            ValueError,
            MigrationRunPlanningError,
            WorkspaceError,
        ) as error:
            rejected = await _reject_mapping_mutation(
                context,
                workspace_id,
                operation_id,
                mutation_started=mutation_started,
                failure_code=_mapping_mutation_failure_code(error),
                failure_detail=_mapping_mutation_failure_detail(error),
            )
            if (
                json_request
                and rejected is not None
                and rejected.state is MappingMutationState.COMMITTED
            ):
                payload = _mapping_mutation_receipt_payload(
                    request,
                    workspace_id,
                    rejected,
                )
                payload["message"] = (
                    "The Match data change was saved. Reload to continue from "
                    "the committed state."
                )
                return JSONResponse(payload)
            if json_request:
                current_working = (
                    context.queries.get_mapping_working_draft(workspace_id)
                )
                current_revision = context.queries.get_mapping_revision(
                    workspace_id
                )
                set_diagnostic_working_draft_version(
                    request,
                    current_working.version if current_working else None,
                )
                payload = {
                        "detail": str(error),
                        "operation_id": operation_id,
                        "status": "rejected",
                        "failure_code": _mapping_mutation_failure_code(error),
                        "expected_working_draft_version": (
                            current_working.version if current_working else None
                        ),
                        "expected_parent_version": (
                            current_revision.version if current_revision else None
                        ),
                    }
                if rejected is not None:
                    payload.update(rejected.portable_dict())
                    payload["detail"] = str(error)
                return JSONResponse(payload, status_code=422)
            request.session["mapping_error"] = str(error)
            return RedirectResponse(
                mapping_return_url,
                status_code=303,
            )
        if json_request:
            return JSONResponse(
                await _mapping_mutation_result_payload(
                    context,
                    request,
                    workspace_id,
                    operation_id,
                    message=message,
                    redirect_url=mapping_return_url,
                )
            )
        return RedirectResponse(
            mapping_return_url,
            status_code=303,
        )

    @router.post(
        "/workspaces/{workspace_id}/mapping/transformation-impact/acknowledge"
    )
    async def acknowledge_transformation_rule(
        request: Request,
        workspace_id: str,
    ):
        """Acknowledge one zero-match or overlap fact for the current snapshot."""

        require_session(request)
        _require_mapping_idle(context, workspace_id)
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "rule_fingerprint"})
        try:
            await run_in_threadpool(
                context.transformation_impacts.acknowledge_rule,
                workspace_id,
                _text(form, "rule_fingerprint"),
                actor=context.actor,
            )
        except WorkspaceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        _flash(request, "The rule result was reviewed.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/mapping/transformation-impact",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/mapping/review-workbook")
    async def create_mapping_review_workbook(
        request: Request,
        workspace_id: str,
    ):
        """Create a workbook from the exact current Stage 3 check."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        _require_mapping_idle(context, workspace_id)
        try:
            revision, validation, selection, schema = (
                _current_mapping_review_evidence(context, workspace_id)
            )
            access = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PROTECTED_EVIDENCE_MANAGE,
            )
            filename = mapping_review_workbook_name(revision)

            def write_workbook() -> None:
                with context.artifacts.prepare_report(
                    workspace_id,
                    access.migration_run_id,
                    filename,
                ) as workbook_path:
                    write_mapping_review_workbook(
                        revision,
                        validation,
                        selection,
                        schema,
                        workbook_path,
                    )

            await run_in_threadpool(write_workbook)
        except (
            ArtifactStoreError,
            MappingReviewGenerationError,
            OSError,
            WorkspaceError,
        ) as error:
            return await run_in_threadpool(
                _render_mapping,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Matching review workbook created.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/mapping",
            status_code=303,
        )

    @router.get("/workspaces/{workspace_id}/mapping/review-workbook")
    async def download_mapping_review_workbook(
        request: Request,
        workspace_id: str,
    ):
        """Download only the workbook for the exact current Stage 3 check."""

        require_session(request)
        revision, _validation, _selection, _schema = (
            _current_mapping_review_evidence(context, workspace_id)
        )
        access = context.workspace_access.resolve(
            workspace_id,
            actor=context.actor,
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )
        filename = mapping_review_workbook_name(revision)
        try:
            exists = context.artifacts.report_exists(
                workspace_id,
                access.migration_run_id,
                filename,
            )
        except ArtifactStoreError as error:
            raise HTTPException(
                status_code=404,
                detail="Matching review workbook not found",
            ) from error
        if not exists:
            raise HTTPException(
                status_code=404,
                detail="Matching review workbook not found",
            )
        return StreamingResponse(
            _mapping_review_chunks(
                context,
                workspace_id,
                access.migration_run_id,
                filename,
            ),
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="impodo-{workspace_id[:8]}-'
                    'matching-review.xlsx"'
                )
            },
        )

    return router


def _mapping_catalog_search_generation(
    request: Request,
) -> tuple[str, int] | None:
    editor_value = request.query_params.get("editor_id", "").strip()
    generation_value = request.query_params.get("generation", "").strip()
    if not editor_value and not generation_value:
        return None
    if not editor_value or not generation_value:
        raise HTTPException(
            status_code=422,
            detail="Field search identity is incomplete",
        )
    try:
        editor_id = str(UUID(editor_value))
        generation = int(generation_value)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=422,
            detail="Field search identity is invalid",
        ) from error
    if generation < 1 or generation > 2_147_483_647:
        raise HTTPException(
            status_code=422,
            detail="Field search generation is invalid",
        )
    return editor_id, generation


def _append_server_timing(response, name: str, duration_ms: float) -> None:
    timing = f"{name};dur={max(0.0, duration_ms):.1f}"
    existing = response.headers.get("Server-Timing", "")
    response.headers["Server-Timing"] = (
        f"{existing}, {timing}" if existing else timing
    )


def _confirmed_recipe_mapping_destination(application) -> str:
    """Continue through the run while another Recipe review remains."""

    if application.status is RecipeApplicationStatus.BLOCKED:
        return (
            f"/projects/{application.project_id}/runs/"
            f"{application.migration_run_id}"
        )
    return f"/workspaces/{application.workspace_id}/prepare"


def _active_preparation_url(context: WebContext, workspace_id: str) -> str:
    manager = context.preparation_jobs
    active = manager.active(workspace_id) if manager is not None else None
    if active is None:
        return ""
    return f"/workspaces/{workspace_id}/preparation/{active.job_id}"


def _require_mapping_idle(context: WebContext, workspace_id: str) -> None:
    if _active_preparation_url(context, workspace_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Preparation is using this workspace's saved data. Wait for it "
                "to finish before reviewing or changing field matches."
            ),
        )


def _mapping_mutation_action(action: str) -> MappingMutationAction:
    if action == "save_progress":
        return MappingMutationAction.SAVE_PROGRESS
    if action == "draft":
        return MappingMutationAction.CHECK_MATCHES
    if action == "submit":
        return MappingMutationAction.CONFIRM_MATCHES
    if action == "remove_readonly":
        return MappingMutationAction.REMOVE_READONLY
    if action == "confirm_defaults":
        return MappingMutationAction.CONFIRM_DEFAULTS
    if action == "refresh_defaults":
        return MappingMutationAction.REFRESH_DEFAULTS
    if action.startswith("set_disposition:"):
        return MappingMutationAction.SET_DISPOSITION
    if action.startswith("clear_disposition:"):
        return MappingMutationAction.CLEAR_DISPOSITION
    raise WorkspaceError("Choose a supported matching action")


def _mapping_mutation_request_hash(form) -> str:
    """Bind one operation identity to exact non-secret submitted meaning."""

    pairs = sorted(
        (str(name), str(value))
        for name, value in form.multi_items()
        if name not in {"csrf_token", "operation_id"}
    )
    payload = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping_mutation_receipt_payload(
    request: Request,
    workspace_id: str,
    receipt: MappingMutationReceipt,
) -> dict[str, object]:
    payload = receipt.portable_dict()
    payload["redirect_url"] = _mapping_return_url(request, workspace_id)
    if receipt.state is MappingMutationState.PENDING:
        payload["message"] = (
            "Impodo is still resolving this operation. Do not repeat it yet; "
            "check the save outcome again."
        )
    elif receipt.state is MappingMutationState.COMMITTED:
        completed = receipt.completed_at
        completed_label = completed.isoformat() if completed is not None else ""
        payload["saved_at"] = completed_label
        payload["message"] = (
            f"Saved {completed_label}."
            if completed_label
            else "The Match data change was saved."
        )
    elif receipt.failure_code == MappingVersionConflict.code:
        payload["detail"] = (
            "This page is out of date because newer Match data was saved. "
            "Your edits are still on this page."
        )
        payload["message"] = str(payload["detail"])
        payload["recovery"] = {
            "reload_url": _mapping_return_url(request, workspace_id),
            "copy_edits": True,
        }
    else:
        payload["message"] = receipt.failure_detail or (
            "This operation did not complete. Your edits remain on this page."
        )
    return payload


async def _mapping_mutation_result_payload(
    context: WebContext,
    request: Request,
    workspace_id: str,
    operation_id: str,
    *,
    message: str,
    redirect_url: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    receipt = await run_in_threadpool(
        context.mapping_workspace.get_mutation_receipt,
        workspace_id,
        operation_id,
        actor=context.actor,
    )
    if receipt is None or receipt.state is not MappingMutationState.COMMITTED:
        raise WorkspaceError(
            "Impodo could not verify the saved Match data operation receipt"
        )
    payload = _mapping_mutation_receipt_payload(
        request,
        workspace_id,
        receipt,
    )
    payload.update(
        {
            "message": message,
            "redirect_url": redirect_url,
        }
    )
    if extra:
        payload.update(extra)
    return payload


async def _reject_mapping_mutation(
    context: WebContext,
    workspace_id: str,
    operation_id: str,
    *,
    mutation_started: bool,
    failure_code: str,
    failure_detail: str,
) -> MappingMutationReceipt | None:
    if not mutation_started or not operation_id:
        return None
    try:
        return await run_in_threadpool(
            context.mapping_workspace.reject_mutation,
            workspace_id,
            operation_id,
            failure_code=failure_code,
            failure_detail=failure_detail,
            actor=context.actor,
        )
    except Exception:
        # Cleanup must never hide the original command failure. A surviving
        # PENDING receipt correctly leaves the browser outcome as unknown.
        return None


def _mapping_mutation_failure_code(error: Exception) -> str:
    if isinstance(error, (ConnectorError, SecretStoreError)):
        return "ODOO_READ_FAILED"
    if isinstance(error, MigrationRunPlanningError):
        return "MAPPING_CONFIRMATION_FAILED"
    if isinstance(error, ValueError):
        return "MAPPING_INPUT_INVALID"
    return "MAPPING_COMMAND_REJECTED"


def _mapping_mutation_failure_detail(error: Exception) -> str:
    if isinstance(error, (ConnectorError, SecretStoreError)):
        return (
            "Impodo could not read the required Odoo details. Check the Odoo "
            "connection and try again."
        )
    return str(error)


async def _check_saved_default_decision(
    context: WebContext,
    workspace_id: str,
    working_draft: MappingWorkingDraft,
    *,
    expected_parent_version: int | None,
    operation_id: str | None = None,
) -> tuple[MappingWorkingDraft, MappingValidationResult]:
    """Validate saved Odoo-default decisions before returning to the page."""

    _revision, validation = await run_in_threadpool(
        context.mapping_workspace.check_definition,
        workspace_id,
        datasets=working_draft.definition.datasets,
        expected_parent_version=expected_parent_version,
        expected_working_draft_version=working_draft.version,
        actor=context.actor,
        operation_id=operation_id,
    )
    checked_draft = await run_in_threadpool(
        context.mapping_workspace.mappings.get_mapping_working_draft,
        workspace_id,
    )
    if checked_draft is None:
        raise WorkspaceError("The checked matching draft is no longer available")
    return checked_draft, validation


def _checked_default_message(
    action_message: str,
    validation: MappingValidationResult,
) -> str:
    if validation.status.value == "INVALID":
        return (
            f"{action_message} Matches checked. "
            "Review the remaining items that need attention."
        )
    return f"{action_message} Matches checked and ready to confirm."


def _checked_mapping_return_url(
    request: Request,
    workspace_id: str,
    validation: MappingValidationResult,
    *,
    success_url: str,
) -> str:
    if validation.status.value == "INVALID":
        return (
            f"{_mapping_return_url(request, workspace_id)}"
            "#next-step-blockers"
        )
    return success_url


def _current_mapping_review_evidence(context: WebContext, workspace_id: str):
    """Return one exact checked revision or reject stale Stage 3 evidence."""

    selection = context.queries.get_mapping_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    governance = context.queries.get_schema_governance(workspace_id)
    revision = context.queries.get_mapping_revision(workspace_id)
    if selection is None or schema is None or revision is None:
        raise WorkspaceError("Check matches before creating the workbook")
    validation = context.queries.get_mapping_validation(
        workspace_id,
        revision.version,
    )
    if validation is None:
        raise WorkspaceError("Check matches before creating the workbook")
    expected_schema_hash = (
        governance.content_hash if governance is not None else schema.content_hash
    )
    if (
        revision.definition.source_selection_hash != selection.content_hash
        or revision.definition.schema_hash != expected_schema_hash
        or validation.mapping_content_hash != revision.definition.content_hash
        or validation.source_selection_hash != selection.content_hash
        or validation.schema_hash != expected_schema_hash
    ):
        raise WorkspaceError(
            "Source data or Odoo fields changed. Check matches again before "
            "creating the workbook."
        )
    working = context.queries.get_mapping_working_draft(workspace_id)
    if (
        working is not None
        and working.definition.source_selection_hash == selection.content_hash
        and working.definition.schema_hash == expected_schema_hash
        and working.content_hash != revision.definition.content_hash
    ):
        raise WorkspaceError(
            "Saved field changes have not been checked. Check matches again "
            "before creating the workbook."
        )
    return revision, validation, selection, schema


def _mapping_review_chunks(
    context: WebContext,
    workspace_id: str,
    run_id: str,
    filename: str,
) -> Iterator[bytes]:
    """Stream one contained workbook without loading it completely in memory."""

    with (
        context.artifacts.materialize_report(workspace_id, run_id, filename) as path,
        path.open("rb") as workbook,
    ):
        while chunk := workbook.read(64 * 1024):
            yield chunk
