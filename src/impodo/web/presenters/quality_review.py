"""Label prepared record values and explain source-match findings for review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from impodo.domain.preparation.quality import QualityCauseCount, QualityReviewItem
from impodo.domain.workspace.contracts import OdooSchemaCatalog


def source_match_message(cause: QualityCauseCount, fields: Sequence[str]) -> str:
    """Explain only source matches proven missing for the complete saved cause."""

    field_text = (
        cause.missing_match_field_label or ", ".join(fields) or "The record identifier"
    )
    return (
        f"No matching row in {cause.missing_match_table}. "
        f"{field_text} is empty, so these records are set aside."
    )


def quality_record_details(
    items: Sequence[QualityReviewItem],
    schema: OdooSchemaCatalog | None,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Show available prepared scalar and relationship values with field labels."""

    labels = {
        model.name: {field.name: field.label for field in model.fields}
        for model in (schema.models if schema else ())
    }
    result = {}
    for item in items:
        prepared = item.prepared_row
        if prepared is None:
            continue
        fields = dict(item.prepared_field_labels) or labels.get(
            prepared.target_model, {}
        )
        values = dict(item.prepared_identity_values)
        values.update(prepared.proposed_values)
        values = {
            field: " / ".join(str(part) for part in value.key if part is not None)
            if hasattr(value, "key")
            else value
            for field, value in values.items()
        }
        values.update(
            {
                field: " / ".join(
                    str(value) for value in reference.key if value is not None
                )
                for field, reference in prepared.references.items()
                if hasattr(reference, "key")
            }
        )
        category_path = item.prepared_category_path
        if category_path is not None:
            if not category_path:
                values["parent_id"] = "Unresolved"
            elif len(category_path) > 1:
                values["parent_id"] = category_path[-2]
        result[item.row.row_id] = tuple(
            (
                fields.get(field, field.replace("_", " ").removesuffix(" id").title()),
                ("Yes" if value else "No") if isinstance(value, bool) else str(value),
            )
            for field, value in sorted(
                values.items(), key=lambda pair: (pair[0] != "name", pair[0])
            )
            if value is not None and value != ""
        )
    return result


def quality_review_findings(
    items: Sequence[QualityReviewItem],
    causes: Sequence[QualityCauseCount],
    field_labels: dict[str, dict[str, str]],
    issue_messages: Mapping[str, str] | None = None,
) -> dict[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Group explained empty-key symptoms while retaining raw support evidence."""

    messages = issue_messages or {}
    by_signature = {
        (
            cause.dataset,
            cause.reason_code,
            cause.affected_fields,
            cause.family,
            cause.policy,
            cause.owner_label,
        ): cause
        for cause in causes
    }
    result = {}
    for item in items:
        findings = []
        for issue in item.issues:
            cause = by_signature.get(
                (
                    issue.dataset,
                    issue.reason_code,
                    issue.affected_fields,
                    issue.family,
                    issue.policy,
                    issue.owner_label,
                ),
            )
            if cause is not None and cause.explained_by_cause_key:
                continue
            labels = field_labels.get(issue.dataset, {})
            fields = (
                (cause.missing_match_field_label,)
                if cause and cause.missing_match_field_label
                else tuple(labels.get(field, field) for field in issue.affected_fields)
            )
            message = (
                source_match_message(cause, fields).replace(
                    "these records are", "this record is"
                )
                if cause is not None and cause.missing_match_table
                else messages.get(issue.issue_id, issue.message)
            )
            findings.append((message, fields))
        result[item.row.row_id] = tuple(findings)
    return result
