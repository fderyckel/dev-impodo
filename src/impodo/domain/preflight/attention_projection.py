"""Project saved preparation and comparison findings into a correction worklist.

No checks are rerun here. Duplicate peers are joined using the same complete
identity semantics as preparation, and restricted values are removed before
an artifact adapter receives the projection.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from impodo.domain.mapping.contracts import DatasetMapping, MappingDefinition
from impodo.domain.preparation.quality import (
    QualityOutcomePolicy,
    QualityRun,
    quality_identity_key,
)
from impodo.domain.preparation.staging_contracts import (
    CanonicalRow,
    CanonicalStagingRun,
    StagingDisposition,
)
from impodo.domain.shared.models import (
    BusinessReference,
    InvalidPreparedValue,
    LogicalReference,
    canonical_json_bytes,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection

from .attention import (
    AttentionExport,
    AttentionFinding,
    AttentionRecord,
    attention_guidance,
)


def attention_value(value: object) -> str:
    """Show business values without exposing internal reference representations."""

    if value is None or value == "":
        return "(empty)"
    if isinstance(value, InvalidPreparedValue):
        return "Could not prepare this value"
    if isinstance(value, (LogicalReference, BusinessReference)):
        return attention_value((*value.key, *value.scope))
    if isinstance(value, (tuple, list)):
        return " / ".join(attention_value(part) for part in value)
    if isinstance(value, Mapping):
        return "; ".join(
            f"{key}: {attention_value(part)}" for key, part in value.items()
        )
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _prepared_values(
    row: CanonicalRow, mapping: DatasetMapping | None
) -> dict[str, object]:
    """Include identity and scope fields as well as ordinary mapped values."""
    values: dict[str, object] = dict(row.proposed_values)
    values.update(row.references)
    if mapping is not None:
        for components, parts in (
            (mapping.target_identity, row.target_identity),
            (mapping.target_scope, row.target_scope),
        ):
            fields = [
                field for component in components for field in component.target_fields
            ]
            if len(fields) == len(parts):
                values.update(zip(fields, parts, strict=True))
    return values


def project_attention_export(
    header: AttentionExport,
    staging: CanonicalStagingRun,
    quality: QualityRun,
    definition: MappingDefinition,
    selection: SourceSelection,
    schema: OdooSchemaCatalog | None,
    manifest: Mapping[str, Any],
    *,
    prepared_rows: tuple[CanonicalRow, ...] | None = None,
) -> AttentionExport:
    """Retain every distinct actionable finding and its affected prepared row."""

    datasets = {dataset.dataset_id: dataset for dataset in selection.datasets}
    mappings = {
        datasets[item.dataset_id].name: item
        for item in definition.datasets
        if item.dataset_id in datasets
    }
    labels = {
        model.name: {field.name: field.label for field in model.fields}
        for model in (schema.models if schema else ())
    }
    checked_rows = prepared_rows if prepared_rows is not None else staging.rows
    rows = {row.row_id: row for row in checked_rows}
    coordinates: dict[tuple[str, int], list[str]] = defaultdict(list)
    source_groups: dict[bytes, list[str]] = defaultdict(list)
    target_groups: dict[bytes, list[str]] = defaultdict(list)
    for row in checked_rows:
        coordinates[(row.dataset, row.source_row)].append(row.row_id)
        source_groups[canonical_json_bytes((row.dataset, row.source_identity))].append(
            row.row_id
        )
        identity = quality_identity_key(row)
        if identity is not None:
            target_groups[identity].append(row.row_id)

    findings: dict[tuple[str, str, str, tuple[str, ...], str], AttentionFinding] = {}
    grouped_sources = {key: tuple(members) for key, members in source_groups.items()}
    grouped_targets = {key: tuple(members) for key, members in target_groups.items()}
    group_ids: dict[tuple[str, bytes], str] = {}

    def add(
        row_id: str,
        dataset: str,
        fields: tuple[str, ...],
        code: str,
        message: str,
        priority: str,
        owner: str = "Data manager",
        cause: str = "",
    ) -> None:
        row = rows.get(row_id)
        mapping = mappings.get(dataset)
        related = (cause,) if cause else ()
        group_id = ""
        if row is not None and mapping is not None:
            if code == "SOURCE_IDENTITY_DUPLICATE":
                fields = tuple(mapping.source_identity_column_keys)
                identity = canonical_json_bytes((row.dataset, row.source_identity))
                related = grouped_sources[identity]
                group_id = group_ids.setdefault(
                    (code, identity), f"D{len(group_ids) + 1:04}"
                )
                message = f"The complete source row identifier is repeated: {attention_value(row.source_identity)}. Review every related row."
            elif code == "SOURCE_IDENTITY_INVALID":
                if not fields or set(fields) <= set(
                    mapping.source_identity_column_keys
                ):
                    fields = tuple(mapping.source_identity_column_keys)
                    message = "The selected source row identifier is empty or invalid. Each included source row needs a complete identifier."
                else:
                    message = "An identifying value for the Odoo matching rule is empty or invalid. Complete or correct the affected matching column."
            elif code == "POST_TRANSFORM_IDENTITY_COLLISION":
                fields = tuple(
                    field
                    for component in (*mapping.target_identity, *mapping.target_scope)
                    for field in component.target_fields
                )
                identity = quality_identity_key(row)
                related = grouped_targets.get(identity, ()) if identity else ()
                if identity:
                    group_id = group_ids.setdefault(
                        (code, identity), f"D{len(group_ids) + 1:04}"
                    )
                message = (
                    "After the saved rules, these records use the same complete Odoo match: "
                    f"{attention_value((*row.target_identity, *row.target_scope))}. "
                    "Review every matching column, including company or parent where shown."
                )
            elif code == "TARGET_IDENTITY_AMBIGUOUS":
                fields = tuple(
                    field
                    for component in (*mapping.target_identity, *mapping.target_scope)
                    for field in component.target_fields
                )
                message = f"The complete selected match {attention_value((*row.target_identity, *row.target_scope))} matched more than one Odoo record in the checked company. Impodo cannot choose the intended record."
            elif code in {"REFERENCE_NOT_FOUND", "REFERENCE_AMBIGUOUS"}:
                field_values = _prepared_values(row, mapping)
                description = (
                    "; ".join(
                        f"{labels.get(row.target_model, {}).get(field, field)}: {attention_value(field_values.get(field))}"
                        for field in fields
                    )
                    or "The selected related value"
                )
                message = f"{description} " + (
                    "matched no related record in the Odoo data checked for this company. Check the source value and company before deciding whether a new record is needed."
                    if code == "REFERENCE_NOT_FOUND"
                    else "matched several related records in the checked Odoo data. Review the matching records and choose the intended one."
                )
        problem, action, correction = attention_guidance(code)
        if (
            code == "SOURCE_IDENTITY_INVALID"
            and mapping
            and fields
            and not set(fields) <= set(mapping.source_identity_column_keys)
        ):
            problem = "Odoo matching value is empty or invalid"
            action = "Complete the affected identifying value or review its Odoo matching rule, then prepare the data again."
            correction = "Source data or Match data → Odoo matching rule"
        if cause:
            cause_row = rows.get(cause)
            message = (
                f"This record is held back by {cause_row.dataset}, dataset row {cause_row.source_row}. "
                "Review that record's findings first."
                if cause_row
                else "A linked record needs correction first. Its source location was not retained."
            )
        if header.restricted:
            message = problem + ". Protected values remain inside Impodo."
        effect = (
            "Held back by a linked record"
            if cause
            else (
                "Review the saved decision"
                if priority == "Review"
                else "Needs correction before inclusion"
            )
        )
        key = (
            row_id,
            dataset,
            code,
            tuple(sorted(fields)),
            message if code == "BUSINESS_CHECK_FAILED" else "",
        )
        candidate = AttentionFinding(
            row_id,
            dataset,
            fields,
            priority,
            problem,
            message,
            action,
            correction,
            owner,
            effect,
            code,
            related,
            group_id,
        )
        previous = findings.get(key)
        if previous is not None:
            related = (
                previous.related_rows
                if previous.related_rows == related
                else tuple(sorted({*previous.related_rows, *related}))
            )
            candidate = replace(
                candidate if priority == "Must fix" else previous, related_rows=related
            )
        findings[key] = candidate

    excluded = {
        row.row_id
        for row in checked_rows
        if row.disposition is StagingDisposition.EXCLUDED
    }
    for issue in quality.issues:
        if issue.policy is QualityOutcomePolicy.ADVISORY:
            continue
        if issue.row_id in excluded:
            continue
        add(
            issue.row_id or "",
            issue.dataset,
            issue.affected_fields,
            issue.reason_code,
            issue.message,
            "Review" if issue.policy is QualityOutcomePolicy.WARNING else "Must fix",
            issue.owner_label,
            issue.caused_by_row_id or "",
        )

    for decision in manifest.get("decisions", ()):
        row_id = str(decision.get("source_trace_id") or "")
        dataset = str(decision.get("dataset") or "")
        issues = list(decision.get("issues", ()))
        if decision.get("classification") == "AMBIGUOUS" and not issues:
            issues.append(
                {
                    "code": "TARGET_IDENTITY_AMBIGUOUS",
                    "message": "More than one Odoo record matched the complete selected identifier.",
                    "severity": "error",
                }
            )
        if decision.get("classification") == "BLOCKED" and not issues:
            issues.append(
                {
                    "code": "COMPARISON_BLOCKED",
                    "message": "The saved comparison could not establish a safe outcome for this record.",
                    "severity": "error",
                }
            )
        for issue in issues:
            if issue.get("severity", "error") not in {"error", "warning"}:
                continue
            if row_id in excluded:
                continue
            add(
                row_id,
                dataset,
                (str(issue["field"]),) if issue.get("field") else (),
                str(issue.get("code") or "COMPARISON_BLOCKED"),
                str(issue.get("message") or "Review the saved comparison."),
                "Review" if issue.get("severity") == "warning" else "Must fix",
            )

    for issue in manifest.get("source_issues", ()):
        if issue.get("severity", "error") not in {"error", "warning"}:
            continue
        dataset = str(issue.get("dataset") or "")
        members = coordinates.get((dataset, issue.get("row")), ())
        for row_id in members or ("",):
            if row_id in excluded:
                continue
            add(
                row_id,
                dataset,
                (str(issue["field"]),) if issue.get("field") else (),
                str(issue.get("code") or "COMPARISON_BLOCKED"),
                str(issue.get("message") or "Review the saved comparison."),
                "Review" if issue.get("severity") == "warning" else "Must fix",
            )

    for control in staging.control_totals:
        if not control.passed:
            add(
                "",
                control.dataset,
                (control.target_field,),
                "CONTROL_TOTAL_MISMATCH",
                f"{control.name}: expected {control.expected_total}; prepared {control.actual_total}; difference {control.difference}; allowed difference {control.tolerance}. Review the source values and the declared total.",
                "Must fix",
            )

    affected = {item.row_id for item in findings.values() if item.row_id}
    # Include causal records as context even if historical evidence lacks their finding.
    seen_groups = set()
    for item in findings.values():
        if item.group_id and item.group_id in seen_groups:
            continue
        seen_groups.add(item.group_id)
        affected.update(related for related in item.related_rows if related in rows)
    records = []
    for row in checked_rows:
        if row.row_id not in affected:
            continue
        mapping = mappings.get(row.dataset)
        values = _prepared_values(row, mapping)
        origins = header.source_origins.get(row.dataset, {})
        field_sources = {
            field: tuple(
                dict.fromkeys(
                    physical for key in keys for physical in origins.get(key, (key,))
                )
            )
            for field, keys in row.lineage.field_sources.items()
        }
        field_sources.update(
            {
                key: origins[key]
                for keys in row.lineage.field_sources.values()
                for key in keys
                if key in origins
            }
        )
        records.append(
            AttentionRecord(
                row.row_id,
                row.dataset,
                "Restricted"
                if header.restricted
                else attention_value(row.source_identity),
                row.source_row,
                row.lineage.physical_sources,
                {
                    name: "Restricted" if header.restricted else attention_value(value)
                    for name, value in sorted(values.items())
                },
                labels.get(row.target_model, {}),
                field_sources,
            )
        )
    missing = affected - rows.keys()
    if missing:
        raise ValueError(
            "Some affected records are missing from the saved preparation. Prepare the data again."
        )
    return replace(
        header,
        records=tuple(records),
        findings=tuple(
            sorted(
                findings.values(),
                key=lambda item: (
                    item.priority != "Must fix",
                    item.dataset,
                    rows[item.row_id].source_row if item.row_id in rows else 0,
                    item.code,
                    item.fields,
                ),
            )
        ),
        filtered_count=len(excluded),
    )
