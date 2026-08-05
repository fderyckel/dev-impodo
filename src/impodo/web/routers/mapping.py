"""Mapping browser routes."""

from __future__ import annotations
import csv
from dataclasses import replace
import hashlib
from io import StringIO
import json
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from ...artifacts import ArtifactStoreError
from ...connectors import ConnectorError
from ...projects import ProjectError
from ...domain.errors import ReadinessError
from ...domain.staging.transformation_impact import TransformationImpactFilter
from ...secrets import SecretStoreError
from ...source import SourceLoadError
from ...workspace import WorkspaceError
from ..security import require_csrf, require_session
from fastapi import APIRouter
from ..constants import TRANSFORMATION_IMPACT_PAGE_SIZE, TRANSFORMATION_IMPACT_OUTCOMES
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
    _transformation_impact_url,
)
from ..presenters.mapping_view import _render_mapping, _safe_spreadsheet_text
from ..target_readers import _relationship_value_choices, _source_value_choices


def build_mapping_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{project_id}/mapping", response_class=HTMLResponse)
    async def project_mapping(request: Request, project_id: str):
        require_session(request)
        return _render_mapping(request, context, project_id)

    @router.post("/projects/{project_id}/mapping/value-choices")
    async def project_mapping_value_choices(
        request: Request,
        project_id: str,
    ):
        """Return bounded source and Odoo choices for the mapping dialog."""

        require_session(request)
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
            },
        )
        try:
            kind = _text(form, "kind")
            if kind not in {"scalar", "relationship"}:
                raise WorkspaceError("Choose a supported Odoo field")
            selection = context.queries.get_mapping_source_selection(
                project_id
            )
            schema = context.queries.get_odoo_schema_catalog(project_id)
            governance = context.queries.get_schema_governance(project_id)
            if selection is None or schema is None or governance is None:
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
                project_id,
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
            if kind == "scalar":
                if not field.selection:
                    raise WorkspaceError(
                        "This Odoo field does not provide a list of choices"
                    )
                target_choices = tuple(
                    {
                        "value": str(value),
                        "label": str(label),
                    }
                    for value, label in field.selection
                )
            else:
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
                project = context.queries.get(project_id)
                target_choices, ambiguous_values = await run_in_threadpool(
                    _relationship_value_choices,
                    context,
                    project,
                    schema,
                    field,
                    key,
                )
            return JSONResponse(
                {
                    "source_choices": source_choices,
                    "target_choices": target_choices,
                    "ambiguous_values": ambiguous_values,
                }
            )
        except (
            ArtifactStoreError,
            ConnectorError,
            ProjectError,
            SecretStoreError,
            SourceLoadError,
            WorkspaceError,
        ) as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    @router.get(
        "/projects/{project_id}/mapping/transformation-impact",
        response_class=HTMLResponse,
    )
    async def project_transformation_impact(request: Request, project_id: str):
        """Render one bounded page from prepared transformation evidence."""

        require_session(request)
        try:
            evidence = _transformation_impact_evidence(context, project_id)
        except WorkspaceError as error:
            return _render_mapping(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        project, revision, physical_selection, effective_selection, plan = evidence
        identity = _transformation_impact_identity(
            revision,
            physical_selection,
            effective_selection,
            plan,
        )
        snapshot = context.queries.get_transformation_impact_snapshot(
            project_id,
            identity,
        )
        datasets, field_labels, field_choices = _transformation_impact_labels(
            context,
            project_id,
            revision,
            effective_selection,
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
                project_id,
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
                project_id,
                identity,
                filters,
                page_size=TRANSFORMATION_IMPACT_PAGE_SIZE,
                after=after,
                before=before,
            )
            rows = page.rows
            if page.previous_before is not None:
                previous_url = _transformation_impact_url(
                    project_id,
                    filters,
                    before=page.previous_before,
                )
            if page.next_after is not None:
                next_url = _transformation_impact_url(
                    project_id,
                    filters,
                    after=page.next_after,
                )
        return _render(
            request,
            "project_transformation_impact.html",
            project=project,
            revision=revision,
            snapshot=snapshot,
            report=snapshot.report if snapshot is not None else None,
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
        "/projects/{project_id}/mapping/transformation-impact/prepare"
    )
    async def prepare_transformation_impact(request: Request, project_id: str):
        """Prepare one hash-bound local snapshot without contacting Odoo."""

        require_session(request)
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            evidence = _transformation_impact_evidence(context, project_id)
        except WorkspaceError as error:
            return _render_mapping(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        project, revision, physical_selection, effective_selection, plan = evidence
        identity = _transformation_impact_identity(
            revision,
            physical_selection,
            effective_selection,
            plan,
        )

        try:
            await run_in_threadpool(
                context.transformation_impacts.prepare_snapshot,
                project_id,
                actor=context.actor,
            )
        except (OSError, ReadinessError, WorkspaceError) as error:
            datasets, field_labels, field_choices = _transformation_impact_labels(
                context,
                project_id,
                revision,
                effective_selection,
            )
            return _render(
                request,
                "project_transformation_impact.html",
                project=project,
                revision=revision,
                snapshot=None,
                report=None,
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
            f"/projects/{project_id}/mapping/transformation-impact",
            status_code=303,
        )

    @router.post("/projects/{project_id}/mapping/transformation-impact.csv")
    async def download_transformation_impact(request: Request, project_id: str):
        """Download matching persisted impact rows without recomputing them."""

        require_session(request)
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "dataset", "outcome", "field", "q"},
        )
        try:
            evidence = _transformation_impact_evidence(context, project_id)
        except WorkspaceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        _project, revision, physical_selection, effective_selection, plan = evidence
        identity = _transformation_impact_identity(
            revision,
            physical_selection,
            effective_selection,
            plan,
        )
        datasets, _field_labels, field_choices = _transformation_impact_labels(
            context,
            project_id,
            revision,
            effective_selection,
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
            project_id,
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
                    project_id,
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

    @router.post("/projects/{project_id}/mapping/save")
    async def save_project_mapping(request: Request, project_id: str):
        require_session(request)
        json_request = _is_json_request(request)
        selection = context.queries.get_mapping_source_selection(project_id)
        if selection is None:
            raise HTTPException(status_code=422, detail="Source selection missing")

        schema = context.queries.get_odoo_schema_catalog(project_id)
        governance = context.queries.get_schema_governance(project_id)
        if schema is None:
            raise HTTPException(status_code=422, detail="Odoo schema missing")
        try:
            form = await _mapping_request_form(request)
            if json_request:
                require_csrf(request, request.headers.get("x-csrf-token", ""))
            allowed = _mapping_allowed_fields(form, selection, schema)
            _secure_form(request, form, allowed)
            action = _text(form, "action")
            if action not in {"save_progress", "draft", "submit"}:
                raise WorkspaceError(
                    "Choose save progress, validate draft, or submit"
                )
            expected_parent = _optional_int(
                _text(form, "expected_parent_version")
            )
            expected_working_version = _optional_int(
                _text(form, "expected_working_draft_version")
            )
            datasets = _mapping_datasets_from_form(
                form,
                selection,
                schema,
                governance,
            )
            datasets = _merge_partial_mapping_datasets(
                datasets,
                _active_mapping_definition(
                    context,
                    project_id,
                    selection,
                    schema,
                    governance,
                ),
                form,
                selection,
                schema,
            )
            working_draft = await run_in_threadpool(
                context.mapping_workspace.save_working_draft,
                project_id,
                datasets=datasets,
                expected_version=expected_working_version,
                actor=context.actor,
            )
            if action == "save_progress":
                _flash(
                    request,
                    (
                        "Saved your matching progress. The matches have not been checked yet."
                    ),
                )
                if json_request:
                    return JSONResponse(
                        {
                            "message": (
                                f"Saved working draft version "
                                f"{working_draft.version}."
                            ),
                            "redirect_url": _mapping_return_url(
                                request,
                                project_id,
                            ),
                        }
                    )
                return RedirectResponse(
                    _mapping_return_url(request, project_id),
                    status_code=303,
                )
            revision, validation, submission = await run_in_threadpool(
                context.mapping_workspace.save_definition,
                project_id,
                datasets=datasets,
                expected_parent_version=expected_parent,
                submit=action == "submit",
                warning_acknowledgements=_texts(
                    form, "warning_acknowledgement"
                ),
                actor=context.actor,
            )
        except HTTPException as error:
            return _mapping_save_error_response(
                request,
                project_id,
                error,
                json_request=json_request,
            )
        except (ValueError, WorkspaceError) as error:
            if json_request:
                current_working = (
                    context.queries.get_mapping_working_draft(project_id)
                )
                current_revision = context.queries.get_mapping_revision(
                    project_id
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
                _mapping_return_url(request, project_id),
                status_code=303,
            )
        if submission is not None:
            _flash(
                request,
                "Field matches confirmed.",
            )
        else:
            _flash(
                request,
                (
                    "Saved and checked the field matches."
                ),
            )
        if json_request:
            return JSONResponse(
                {
                    "message": (
                        f"Mapping submitted as version {revision.version}."
                        if submission is not None
                        else f"Saved mapping version {revision.version}."
                    ),
                    "redirect_url": _mapping_return_url(request, project_id),
                }
            )
        return RedirectResponse(
            _mapping_return_url(request, project_id),
            status_code=303,
        )

    return router
