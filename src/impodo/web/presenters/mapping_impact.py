"""Transformation-impact presentation helpers."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
)
from ...domain.mapping.contracts import ScalarValueSource
from ..constants import (
    DEFAULT_MAPPING_FIELDS_PER_PAGE,
    MAPPING_FIELD_PAGE_SIZES,
    TRANSFORMATION_IMPACT_OUTCOMES,
)
from ..context import WebContext
from ..forms import _positive_query_int


def _mapping_save_error_response(
    request: Request,
    project_id: str,
    error: HTTPException,
    *,
    json_request: bool,
):
    detail = str(error.detail)
    if json_request:
        return JSONResponse({"detail": detail}, status_code=error.status_code)
    if error.status_code in {400, 413, 415}:
        return_url = _mapping_return_url(request, project_id)
        separator = "&" if "?" in return_url else "?"
        return RedirectResponse(
            f"{return_url}{separator}save_error=request_rejected",
            status_code=303,
        )
    return JSONResponse({"detail": detail}, status_code=error.status_code)


def _transformation_impact_evidence(context: WebContext, project_id: str):
    evidence = context.transformation_impacts.context(project_id)
    return (
        evidence.project,
        evidence.revision,
        evidence.physical_selection,
        evidence.effective_selection,
        evidence.plan,
    )


def _transformation_impact_identity(
    revision,
    physical_selection,
    effective_selection,
    plan,
) -> TransformationImpactIdentity:
    return TransformationImpactIdentity(
        physical_selection_hash=physical_selection.content_hash,
        source_selection_hash=effective_selection.content_hash,
        mapping_content_hash=revision.definition.content_hash,
        schema_hash=revision.definition.schema_hash,
        derived_plan_hash=plan.content_hash if plan is not None else None,
    )


def _transformation_impact_labels(
    context: WebContext,
    project_id: str,
    revision,
    effective_selection,
):
    schema = context.queries.get_odoo_schema_catalog(project_id)
    model_by_name = {
        model.name: model for model in (schema.models if schema else ())
    }
    dataset_by_id = {
        dataset.dataset_id: dataset for dataset in effective_selection.datasets
    }
    field_labels: dict[tuple[str, str], str] = {}
    field_choices: dict[str, str] = {}
    for mapping in revision.definition.datasets:
        dataset = dataset_by_id.get(mapping.dataset_id)
        model = model_by_name.get(mapping.target_model)
        if dataset is None:
            continue
        if model is not None:
            for field in model.fields:
                field_labels[(dataset.name, field.name)] = field.label
        for field in mapping.fields:
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            field_choices.setdefault(
                field.target_field,
                field_labels.get(
                    (dataset.name, field.target_field),
                    field.target_field,
                ),
            )
    datasets = tuple(
        (dataset.name, dataset.name.replace("_", " ").title())
        for dataset in effective_selection.datasets
    )
    return (
        datasets,
        field_labels,
        tuple(sorted(field_choices.items(), key=lambda item: item[1].casefold())),
    )


def _transformation_impact_filters(
    *,
    dataset: str,
    outcome: str,
    target_field: str,
    query: str,
    allowed_datasets: set[str],
    allowed_fields: set[str],
) -> TransformationImpactFilter:
    selected_dataset = dataset.strip()[:128]
    selected_outcome = outcome.strip()[:32]
    selected_field = target_field.strip()[:128]
    return TransformationImpactFilter(
        dataset=(
            selected_dataset if selected_dataset in allowed_datasets else ""
        ),
        outcome=(
            selected_outcome
            if selected_outcome in TRANSFORMATION_IMPACT_OUTCOMES
            else ""
        ),
        target_field=(
            selected_field if selected_field in allowed_fields else ""
        ),
        query=query.strip()[:128],
    )


def _transformation_impact_url(
    project_id: str,
    filters: TransformationImpactFilter,
    *,
    after: int | None = None,
    before: int | None = None,
) -> str:
    parameters = {
        "dataset": filters.dataset,
        "outcome": filters.outcome,
        "field": filters.target_field,
        "q": filters.query,
        "after": str(after) if after is not None else "",
        "before": str(before) if before is not None else "",
    }
    query = urlencode(
        {name: value for name, value in parameters.items() if value}
    )
    base = f"/projects/{project_id}/mapping/transformation-impact"
    return f"{base}?{query}" if query else base


def _mapping_return_url(
    request: Request,
    project_id: str,
    **updates: object,
) -> str:
    allowed_names = {
        "mapping_dataset",
        "scalar_page",
        "scalar_page_size",
        "relation_page",
        "relation_page_size",
        "relation_query",
        "field_query",
        "mapped_only",
    }
    params = {
        name: value
        for name, value in request.query_params.items()
        if (
            name in allowed_names
            or re.fullmatch(r"target_model_\d+", name) is not None
        )
        and len(value) <= 256
    }
    for name, value in updates.items():
        if value is None or value == "":
            params.pop(name, None)
        else:
            params[name] = str(value)
    query = urlencode(params)
    base = f"/projects/{project_id}/mapping"
    return f"{base}?{query}" if query else base


def _mapping_field_page_size(value: str | None) -> int:
    requested = _positive_query_int(
        value,
        default=DEFAULT_MAPPING_FIELDS_PER_PAGE,
    )
    return (
        requested
        if requested in MAPPING_FIELD_PAGE_SIZES
        else DEFAULT_MAPPING_FIELDS_PER_PAGE
    )
