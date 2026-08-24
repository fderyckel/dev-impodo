"""Transformation-impact presentation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    TransformationImpactRow,
    selection_rule_impact_definitions,
    transformation_rule_impact_definitions,
)
from ...domain.mapping.contracts import ScalarValueSource
from ..constants import (
    DEFAULT_MAPPING_FIELDS_PER_PAGE,
    MAPPING_FIELD_PAGE_SIZES,
    TRANSFORMATION_IMPACT_OUTCOMES,
)
from ..context import WebContext
from ..forms import _positive_query_int


@dataclass(frozen=True, slots=True)
class _TransformationImpactRowView:
    """Add plain-language display evidence without changing stored values."""

    row: TransformationImpactRow
    original_spacing_note: str = ""
    prepared_spacing_note: str = ""


def _transformation_impact_row_views(
    rows: tuple[TransformationImpactRow, ...],
) -> tuple[_TransformationImpactRowView, ...]:
    views = []
    for row in rows:
        original_note, prepared_note = _edge_spacing_change_notes(
            row.raw_value,
            row.proposed_value,
        )
        views.append(
            _TransformationImpactRowView(
                row=row,
                original_spacing_note=original_note,
                prepared_spacing_note=prepared_note,
            )
        )
    return tuple(views)


def _edge_spacing_change_notes(
    raw_value: str,
    proposed_value: str,
) -> tuple[str, str]:
    """Explain edge spaces that a browser cannot make reliably visible."""

    if raw_value == proposed_value or not raw_value:
        return "", ""
    if raw_value.isspace():
        return "Contains only spaces.", ""

    leading_removed = max(
        0,
        _leading_spacing_count(raw_value) - _leading_spacing_count(proposed_value),
    )
    trailing_removed = max(
        0,
        _trailing_spacing_count(raw_value) - _trailing_spacing_count(proposed_value),
    )
    if not leading_removed and not trailing_removed:
        return "", ""

    changes = _spacing_change_phrase(leading_removed, trailing_removed)
    return f"Contains {changes}.", f"Removed {changes}."


def _leading_spacing_count(value: str) -> int:
    return len(value) - len(value.lstrip())


def _trailing_spacing_count(value: str) -> int:
    return len(value) - len(value.rstrip())


def _spacing_change_phrase(leading: int, trailing: int) -> str:
    parts = []
    if leading:
        parts.append(
            f"{leading} space{'s' if leading != 1 else ''} before the value"
        )
    if trailing:
        parts.append(
            f"{trailing} space{'s' if trailing != 1 else ''} after the value"
        )
    return " and ".join(parts)


def _mapping_save_error_response(
    request: Request,
    workspace_id: str,
    error: HTTPException,
    *,
    json_request: bool,
):
    detail = str(error.detail)
    if json_request:
        return JSONResponse({"detail": detail}, status_code=error.status_code)
    if error.status_code in {400, 413, 415}:
        return_url = _mapping_return_url(request, workspace_id)
        separator = "&" if "?" in return_url else "?"
        return RedirectResponse(
            f"{return_url}{separator}save_error=request_rejected",
            status_code=303,
        )
    return JSONResponse({"detail": detail}, status_code=error.status_code)


def _transformation_impact_evidence(context: WebContext, workspace_id: str):
    evidence = context.transformation_impacts.context(workspace_id)
    return (
        evidence.workspace_state,
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
    workspace_id: str,
    revision,
    effective_selection,
):
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    model_by_name = {
        model.name: model for model in (schema.models if schema else ())
    }
    dataset_by_id = {
        dataset.dataset_id: dataset for dataset in effective_selection.datasets
    }
    field_labels: dict[tuple[str, str], str] = {}
    selection_labels: dict[tuple[str, str, str], str] = {}
    field_choices: dict[str, str] = {}
    for mapping in revision.definition.datasets:
        dataset = dataset_by_id.get(mapping.dataset_id)
        model = model_by_name.get(mapping.target_model)
        if dataset is None:
            continue
        if model is not None:
            for field in model.fields:
                field_labels[(dataset.name, field.name)] = field.label
                for technical_value, label in field.selection:
                    selection_labels[
                        (dataset.name, field.name, str(technical_value))
                    ] = label
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
        selection_labels,
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
    workspace_id: str,
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
    base = f"/workspaces/{workspace_id}/mapping/transformation-impact"
    return f"{base}?{query}" if query else base


def _mapping_return_url(
    request: Request,
    workspace_id: str,
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
    base = f"/workspaces/{workspace_id}/mapping"
    return f"{base}?{query}" if query else base


def _transformation_rule_impact_views(
    request: Request,
    workspace_id: str,
    snapshot,
    revision,
    selection,
    field_labels,
    selection_labels,
) -> tuple[dict[str, object], ...]:
    """Join persisted rule counts to current business labels and edit links."""

    if snapshot is None:
        return ()
    fields = {
        (dataset.dataset_id, field.target_field): field
        for dataset in revision.definition.datasets
        for field in dataset.fields
    }
    datasets = {
        dataset.dataset_id: (index, dataset)
        for index, dataset in enumerate(selection.datasets)
    }
    acknowledged = frozenset(snapshot.acknowledged_rule_fingerprints)
    cleanup_steps = {}
    selection_steps = {}
    for dataset in revision.definition.datasets:
        for field in dataset.fields:
            definitions = transformation_rule_impact_definitions(
                dataset.dataset_id, field
            )
            authored_steps = (
                (step_index, step)
                for step_index, step in enumerate(
                    field.transform.text_steps
                )
                if step.configured
            )
            for (step_index, step), definition in zip(
                authored_steps, definitions, strict=True
            ):
                cleanup_steps[definition.rule_fingerprint] = (
                    step_index,
                    step,
                )
            selection_definitions = selection_rule_impact_definitions(
                dataset.dataset_id,
                field,
            )
            if field.selection_rules is not None:
                for rule_index, rule in enumerate(field.selection_rules.rules):
                    match_definition = selection_definitions[rule_index * 2]
                    overlap_definition = selection_definitions[rule_index * 2 + 1]
                    selection_steps[match_definition.rule_fingerprint] = (
                        rule_index,
                        rule,
                        overlap_definition.rule_fingerprint,
                    )
    impact_by_fingerprint = {
        impact.rule_fingerprint: impact
        for impact in snapshot.report.rule_impacts
    }
    views = []
    for impact in snapshot.report.rule_impacts:
        configured = fields.get((impact.dataset_id, impact.target_field))
        located = datasets.get(impact.dataset_id)
        if configured is None or located is None:
            continue
        cleanup_step = cleanup_steps.get(impact.rule_fingerprint)
        selection_step = selection_steps.get(impact.rule_fingerprint)
        if cleanup_step is None and selection_step is None:
            continue
        dataset_index, dataset = located
        field_label = field_labels.get(
            (dataset.name, impact.target_field),
            impact.target_field,
        )
        fix_url = _mapping_return_url(
            request,
            workspace_id,
            mapping_dataset=dataset_index,
            scalar_page=1,
            field_query=field_label,
        )
        common = {
            "impact": impact,
            "dataset_index": dataset_index,
            "dataset_name": dataset.name,
            "field_label": field_label,
            "fix_url": f"{fix_url}#mapping-dataset-{dataset_index}",
        }
        if cleanup_step is not None:
            step_index, step = cleanup_step
            views.append(
                {
                    **common,
                    "kind": "cleanup",
                    "step_number": step_index + 1,
                    "search_value": step.search_value,
                    "replacement_value": step.replacement_value,
                    "requires_acknowledgement": impact.requires_acknowledgement,
                    "acknowledged": impact.rule_fingerprint in acknowledged,
                    "acknowledgements": (
                        (
                            {
                                "fingerprint": impact.rule_fingerprint,
                                "label": "Keep this cleanup step",
                            },
                        )
                        if impact.requires_acknowledgement
                        and impact.rule_fingerprint not in acknowledged
                        else ()
                    ),
                }
            )
            continue
        assert selection_step is not None
        rule_index, rule, overlap_fingerprint = selection_step
        overlap_impact = impact_by_fingerprint.get(overlap_fingerprint)
        overlap_count = (
            overlap_impact.matched_value_count
            if overlap_impact is not None
            else 0
        )
        pending_acknowledgements = []
        if (
            impact.requires_acknowledgement
            and impact.rule_fingerprint not in acknowledged
        ):
            pending_acknowledgements.append(
                {
                    "fingerprint": impact.rule_fingerprint,
                    "label": "Keep this unused rule",
                }
            )
        if (
            overlap_impact is not None
            and overlap_impact.requires_acknowledgement
            and overlap_fingerprint not in acknowledged
        ):
            pending_acknowledgements.append(
                {
                    "fingerprint": overlap_fingerprint,
                    "label": "Keep this rule priority",
                }
            )
        required_fingerprints = {
            candidate.rule_fingerprint
            for candidate in (impact, overlap_impact)
            if candidate is not None and candidate.requires_acknowledgement
        }
        views.append(
            {
                **common,
                "kind": "selection",
                "step_number": rule_index + 1,
                "target_value": rule.target_value,
                "target_label": selection_labels.get(
                    (dataset.name, impact.target_field, rule.target_value),
                    rule.target_value,
                ),
                "condition_count": len(rule.conditions),
                "selected_count": impact.changed_value_count,
                "overlap_count": overlap_count,
                "requires_acknowledgement": bool(required_fingerprints),
                "acknowledged": required_fingerprints.issubset(acknowledged),
                "acknowledgements": tuple(pending_acknowledgements),
            }
        )
    return tuple(
        sorted(
            views,
            key=lambda item: (
                item["dataset_index"],
                item["field_label"].casefold(),
                item["step_number"],
            ),
        )
    )


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
