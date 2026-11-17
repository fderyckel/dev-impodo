"""Explain failed linked-record lookups from exact comparison input.

These private operator details are published with the bounded readiness rows.
They never query Odoo, parse issue messages, or infer a missing record from an
unverified prepared value. Routine diagnostic summaries remain value-free.
"""

import json
from collections.abc import Iterable

from impodo.domain.recipe.profile import DatasetSpec
from impodo.domain.shared.models import (
    Decision,
    LogicalReference,
    PreflightResult,
    PreparedRecord,
    canonical_json_bytes,
    portable_value,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog

REFERENCE_ISSUE_STATUSES = {
    "REFERENCE_NOT_FOUND": "NOT_FOUND",
    "REFERENCE_AMBIGUOUS": "AMBIGUOUS",
    "REFERENCE_BLOCKED_BY_DEPENDENCY": "BLOCKED_BY_DEPENDENCY",
    "REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED": "CASE_MISMATCH",
}


def readiness_reference_details(
    result: PreflightResult,
    records: Iterable[PreparedRecord],
    datasets: Iterable[DatasetSpec],
    schema: OdooSchemaCatalog | None,
) -> dict[str, tuple[str, ...]]:
    """Join each failed lookup to its row, ordered key, and complete scope.

    Index each evidence collection once. In multi-value fields, only references
    with the issue's saved resolution status are described as failed. Stable
    source trace IDs avoid attributing another combined record's values to a
    repeated source row number.
    """

    affected = {
        decision.source_trace_id: decision
        for decision in result.decisions
        if decision.source_trace_id
        and any(issue.code in REFERENCE_ISSUE_STATUSES for issue in decision.issues)
    }
    if not affected:
        return {}
    rows = {
        row.source_trace_id: row for row in records if row.source_trace_id in affected
    }
    mappings = {dataset.name: dataset for dataset in datasets}
    labels = {
        model.name: {field.name: field.label for field in model.fields}
        for model in (schema.models if schema else ())
    }
    failed = {
        (
            item.dataset,
            item.field,
            item.status,
            canonical_json_bytes(portable_value(item.reference)),
        )
        for item in result.reference_resolutions
        if item.status in REFERENCE_ISSUE_STATUSES.values()
    }
    details = {}
    for row_id, decision in affected.items():
        record = rows.get(row_id)
        dataset = mappings.get(decision.dataset)
        values = _reference_values(record, dataset) if record and dataset else {}
        details[row_id] = _decision_details(decision, record, values, labels, failed)
    return details


def _reference_values(
    record: PreparedRecord, dataset: DatasetSpec
) -> dict[str, object]:
    """Retain separate identity, scope, and assigned-field lookup intents."""

    values: dict[str, object] = dict(record.references)
    for prefix, components, parts in (
        (
            "target_identity:",
            dataset.target_identity.components,
            record.target_identity,
        ),
        ("scope:", dataset.target_identity.scope, record.target_scope),
    ):
        fields = tuple(
            field for component in components for field in component.target_fields
        )
        if len(fields) == len(parts):
            values.update(
                (prefix + field, part)
                for field, part in zip(fields, parts, strict=True)
            )
    return values


def _decision_details(
    decision: Decision,
    record: PreparedRecord | None,
    values: dict[str, object],
    labels: dict[str, dict[str, str]],
    failed: set[tuple[str, str, str, bytes]],
) -> tuple[str, ...]:
    """Describe every distinct failed linked field, including secondary issues."""

    details: dict[str, None] = {}
    for issue in decision.issues:
        status = REFERENCE_ISSUE_STATUSES.get(issue.code)
        if status is None:
            continue
        field = issue.field or ""
        name = (
            field.split(":", 1)[-1]
            if field.startswith(("target_identity:", "scope:"))
            else field
        )
        label = labels.get(record.target_model, {}).get(name, name) if record else name
        heading = f"{label} ({name})" if label != name else name or "Linked record"
        value = values.get(field)
        candidates = value if isinstance(value, tuple) else (value,)
        matches = tuple(
            candidate
            for candidate in candidates
            if isinstance(candidate, LogicalReference)
            and (
                decision.dataset,
                field,
                status,
                canonical_json_bytes(portable_value(candidate)),
            )
            in failed
        )
        if not matches:
            details[
                f"{heading}: {issue.message} Lookup details were not retained; compare with Odoo again."
            ] = None
        for reference in matches:
            details[_reference_detail(heading, reference, status, labels)] = None
    return tuple(details)


def _reference_detail(
    heading: str,
    reference: LogicalReference,
    status: str,
    labels: dict[str, dict[str, str]],
) -> str:
    """Name one failed lookup's destination, key, and distinct scope values."""

    reason = {
        "NOT_FOUND": "No related record matched",
        "AMBIGUOUS": "Several related records matched",
        "BLOCKED_BY_DEPENDENCY": "The linked record needs correction first",
        "CASE_MISMATCH": "A related record differs only by letter case",
    }[status]
    related_labels = labels.get(reference.model or "", {})
    key = _key_text(reference.target_fields, reference.key, related_labels)
    location = (
        f"Odoo {reference.model}"
        if reference.origin != "incoming"
        else f"incoming table {reference.dataset}"
    )
    detail = f"{heading}: {reason}. Looked for {key} in {location}."
    if reference.scope:
        scope = _key_text(
            reference.target_scope_fields, reference.scope, related_labels
        )
        detail += f" Within {scope}."
    if reference.origin == "target_then_incoming":
        incoming = _key_text((), reference.incoming_key or reference.key, {})
        detail += f" Also checked incoming table {reference.dataset}: {incoming}."
    return detail


def _key_text(
    fields: tuple[str, ...], values: tuple[object, ...], labels: dict[str, str]
) -> str:
    """Preserve exact strings and key component boundaries for operator review."""

    parts = []
    for index, value in enumerate(values):
        name = fields[index] if index < len(fields) else f"Key {index + 1}"
        label = labels.get(name, name)
        heading = f"{label} ({name})" if label != name else name
        display = (
            "(empty)"
            if value is None
            else json.dumps(value, ensure_ascii=False)
            if isinstance(value, str)
            else str(value)
        )
        parts.append(f"{heading}: {display}")
    return "; ".join(parts) or "(empty)"
