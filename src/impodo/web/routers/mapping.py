"""Expose the Stage D mapping editor and its evidence transitions.

Layer: web route. The save action parses the browser form into complete
dataset-centric contracts, first preserves a recoverable working draft, then
optionally asks ``MappingWorkspaceService`` to validate an immutable revision
or submit it. Preview and transformation-impact routes are projections and do
not authorize later migration stages.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from io import StringIO

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from impodo.domain.shared.access import Capability
from ...application.odoo_read_failures import classify_odoo_read_failure
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.domain.odoo.contracts import ConnectorError
from ...domain.errors import ReadinessError
from ...domain.mapping.contracts import TargetFieldHandling
from ...domain.staging.transformation_impact import TransformationImpactFilter
from impodo.domain.run.contracts import (
    MigrationRunPlanningError,
    RecipeApplicationStatus,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateError
from ..constants import (
    TRANSFORMATION_IMPACT_OUTCOMES,
    TRANSFORMATION_IMPACT_PAGE_SIZE,
)
from ..context import WebContext
from ..forms import (
    _is_json_request,
    _mapping_request_form,
    _optional_int,
    _optional_nonnegative_query_int,
    _secure_form,
    _text,
    _texts,
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
from impodo.web.composition.target_readers import (
    _refresh_mapping_odoo_defaults,
    _relationship_value_choices,
    _source_value_choices,
)


def build_mapping_router(context: WebContext) -> APIRouter:
    """Build mapping editor, preview, impact, validation, and submission routes."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/mapping", response_class=HTMLResponse)
    async def workspace_mapping(request: Request, workspace_id: str):
        require_session(request)
        active_url = _active_preparation_url(context, workspace_id)
        if active_url:
            return RedirectResponse(active_url, status_code=303)
        return _render_mapping(request, context, workspace_id)

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
        return await run_in_threadpool(
            _render_mapping_field_catalog,
            request,
            context,
            workspace_id,
        )

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
            return _render_mapping(
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
            return _render_mapping(
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
                )
                message = (
                    "Saved the Odoo-required field decision. "
                    "Check matches again when ready."
                    if handling is not None
                    else "Cleared the Odoo-required field decision."
                )
                decision_return_url = (
                    _mapping_return_url(
                        request,
                        workspace_id,
                        mapping_dataset=dataset_index,
                    )
                    + "#next-step-blockers"
                )
                if json_request:
                    return JSONResponse(
                        {
                            "message": message,
                            "redirect_url": decision_return_url,
                            "expected_working_draft_version": draft.version,
                        }
                    )
                _flash(request, message)
                return RedirectResponse(decision_return_url, status_code=303)
            if action == "remove_readonly":
                working_draft, removed_count = await run_in_threadpool(
                    context.mapping_workspace.remove_readonly_field_mappings,
                    workspace_id,
                    expected_version=expected_working_version,
                    actor=context.actor,
                )
                message = (
                    f"Removed {removed_count} Odoo-managed field "
                    f"match{'es' if removed_count != 1 else ''}. "
                    "Check matches again when ready."
                )
                if json_request:
                    return JSONResponse(
                        {
                            "message": message,
                            "redirect_url": mapping_return_url,
                            "expected_working_draft_version": (
                                working_draft.version
                            ),
                        }
                    )
                _flash(request, message)
                return RedirectResponse(mapping_return_url, status_code=303)
            if action == "confirm_defaults":
                working_draft, confirmed_count = await run_in_threadpool(
                    context.mapping_workspace.confirm_available_odoo_defaults,
                    workspace_id,
                    expected_version=expected_working_version,
                    actor=context.actor,
                )
                message = (
                    f"Confirmed {confirmed_count} Odoo default"
                    f"{'s' if confirmed_count != 1 else ''}. "
                    "Check matches again when ready."
                )
                if json_request:
                    return JSONResponse(
                        {
                            "message": message,
                            "redirect_url": mapping_return_url,
                            "expected_working_draft_version": (
                                working_draft.version
                            ),
                        }
                    )
                _flash(request, message)
                return RedirectResponse(mapping_return_url, status_code=303)
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
                    _flash(request, message)
                    if json_request:
                        return JSONResponse(
                            {
                                "message": message,
                                "redirect_url": schema_review_url,
                                "expected_working_draft_version": (
                                    expected_working_version
                                ),
                                "expected_parent_version": expected_parent,
                            }
                        )
                    return RedirectResponse(schema_review_url, status_code=303)
                working_draft, confirmed_count = await run_in_threadpool(
                    context.mapping_workspace.confirm_available_odoo_defaults,
                    workspace_id,
                    expected_version=expected_working_version,
                    actor=context.actor,
                )
                message = (
                    f"Odoo will decide {confirmed_count} required field"
                    f"{'s' if confirmed_count != 1 else ''} using the defaults "
                    "checked for this target. Check matches again when ready."
                )
                decision_return_url = (
                    f"{_mapping_return_url(request, workspace_id)}"
                    "#next-step-blockers"
                )
                if json_request:
                    return JSONResponse(
                        {
                            "message": message,
                            "redirect_url": decision_return_url,
                            "expected_working_draft_version": (
                                working_draft.version
                            ),
                        }
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
                )
                if json_request:
                    return JSONResponse(
                        {
                            "message": "Progress saved. Check matches when ready.",
                            "redirect_url": mapping_return_url,
                            "expected_working_draft_version": working_draft.version,
                            "saved_at": working_draft.updated_at.isoformat(),
                        }
                    )
                _flash(
                    request,
                    "Saved your matching progress. Check the matches when ready.",
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
        except HTTPException as error:
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
            if json_request:
                current_working = (
                    context.queries.get_mapping_working_draft(workspace_id)
                )
                current_revision = context.queries.get_mapping_revision(
                    workspace_id
                )
                return JSONResponse(
                    {
                        "detail": str(error),
                        "expected_working_draft_version": (
                            current_working.version if current_working else None
                        ),
                        "expected_parent_version": (
                            current_revision.version if current_revision else None
                        ),
                    },
                    status_code=422,
                )
            request.session["mapping_error"] = str(error)
            return RedirectResponse(
                mapping_return_url,
                status_code=303,
            )
        if json_request:
            return JSONResponse(
                {
                    "message": message,
                    "redirect_url": mapping_return_url,
                }
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

    return router


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

