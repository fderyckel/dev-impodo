"""Write the portable Stage 3 matching review workbook.

The workbook projects one immutable mapping revision, its exact validation
result, and an optional target-independent row evaluation over the frozen file
source.  The row view reuses the Stage 3 evaluator and never contacts Odoo.
Stage 4 decisions and the Stage 5 target comparison remain outside this
artifact.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingTargetMode,
    RelationshipMapping,
    RelationshipValueSource,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldHandling,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.mapping.validation.evidence import (
    MappingValidationIssue,
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    portable_value,
)
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.staging.transformation_impact import TransformationImpactRow
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SourceDataset,
    SourceSelection,
)


MAPPING_REVIEW_WORKBOOK_PREFIX = "impodo_mapping_review"

# These values intentionally match the Stage 5 workbook palette.  Keep the
# Stage 5 writer independent so adding this projection cannot alter it.
_COLORS = {
    "brand": "E8473F",
    "charcoal": "494D46",
    "charcoal_dark": "292C28",
    "gray": "868981",
    "paper": "F4F4F1",
    "surface": "FFFFFF",
    "soft": "EEEEEA",
    "line": "D6D7D2",
    "ready": "4D7C5B",
    "ready_text": "315B3B",
    "ready_bg": "EDF7EF",
    "warning": "7D4F00",
    "warning_bg": "FFF5DF",
    "danger": "9F2F2F",
    "danger_bg": "FCE8E7",
    "prepared": "2F628F",
    "prepared_bg": "EAF2FB",
    "white": "FFFFFF",
}
_STYLE_COLORS = {
    "ready": (_COLORS["ready_bg"], _COLORS["ready_text"]),
    "warning": (_COLORS["warning_bg"], _COLORS["warning"]),
    "danger": (_COLORS["danger_bg"], _COLORS["danger"]),
    "prepared": (_COLORS["prepared_bg"], _COLORS["prepared"]),
    "neutral": (_COLORS["soft"], _COLORS["charcoal"]),
}
_THIN_BORDER = Border(bottom=Side(style="thin", color=_COLORS["line"]))
_EXCEL_MAX_ROW = 1_048_576
_EXCEL_MAX_COLUMN = 16_384


class MappingReviewGenerationError(RuntimeError):
    """Raised when current Stage 3 evidence cannot produce a safe workbook."""


@dataclass(frozen=True, slots=True)
class _FieldReview:
    dataset: DatasetMapping
    source_dataset: SourceDataset
    field: SchemaField
    status: str
    style: str
    provider: str
    source_fields: str
    requirement: str
    next_action: str


@dataclass(frozen=True, slots=True)
class MappingReviewRecipeContext:
    """User-facing lineage for a mapping loaded from one Recipe revision."""

    display_name: str
    revision: int

    @property
    def label(self) -> str:
        return f"Recipe {self.display_name} v{self.revision}"


@dataclass(frozen=True, slots=True)
class MappingReviewCell:
    """One proposed row value and its optional raw-to-proposed explanation."""

    value: Any
    style: str = "neutral"
    comment: str = ""


@dataclass(frozen=True, slots=True)
class MappingReviewDataRow:
    """One Stage 3 output row with direct frozen-source lineage."""

    source_table: str
    source_row: str
    target_model: str
    status: str
    status_style: str
    cells: dict[str, MappingReviewCell]


@dataclass(frozen=True, slots=True)
class MappingReviewRowProjection:
    """Complete workbook-ready rows from one exact Stage 3 evaluation."""

    rows: tuple[MappingReviewDataRow, ...]
    changed_cell_count: int


def mapping_review_workbook_name(revision: MappingRevision) -> str:
    """Return the artifact name bound to one exact mapping revision."""

    digest = revision.definition.content_hash.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise MappingReviewGenerationError("Mapping identity is invalid")
    return (
        f"{MAPPING_REVIEW_WORKBOOK_PREFIX}_v{revision.version}_"
        f"{digest[:16].casefold()}.xlsx"
    )


def build_mapping_review_row_projection(
    revision: MappingRevision,
    effective_selection: SourceSelection,
    physical_selection: SourceSelection,
    canonical_rows: Iterable[Any],
    impacts: Iterable[TransformationImpactRow],
) -> MappingReviewRowProjection:
    """Turn exact canonical rows and impacts into workbook notes and cells."""

    effective_by_id = {
        dataset.dataset_id: dataset for dataset in effective_selection.datasets
    }
    mapping_by_name = {
        effective_by_id[mapping.dataset_id].name: mapping
        for mapping in revision.definition.datasets
        if mapping.dataset_id in effective_by_id
    }
    physical_labels = {
        dataset.dataset_id: dataset.name for dataset in physical_selection.datasets
    }
    impact_by_cell = {
        (impact.dataset, impact.source_row, impact.target_field): impact
        for impact in impacts
    }
    rows: list[MappingReviewDataRow] = []
    changed_count = 0
    for row in canonical_rows:
        mapping = mapping_by_name.get(row.dataset)
        if mapping is None:
            continue
        values: dict[str, Any] = {}
        values.update(_identity_values(mapping.target_identity, row.target_identity))
        values.update(_identity_values(mapping.target_scope, row.target_scope))
        values.update(dict(row.proposed_values))
        values.update(
            {
                field.target_field: "Odoo will choose"
                for field in mapping.fields
                if field.value_source is ScalarValueSource.ODOO_DEFAULT
            }
        )
        values.update(
            {
                field: _display_reference_value(value)
                for field, value in row.references.items()
            }
        )
        for disposition in mapping.target_field_dispositions:
            values[disposition.target_field] = (
                "Odoo will choose"
                if disposition.handling is TargetFieldHandling.ODOO_DEFAULT
                else "Odoo manages this field"
            )

        cells: dict[str, MappingReviewCell] = {}
        for target_field, value in values.items():
            impact = impact_by_cell.get(
                (row.dataset, row.source_row, target_field)
            )
            if impact is None:
                cells[target_field] = MappingReviewCell(
                    value=_display_reference_value(value)
                )
                continue
            changed_count += 1
            style = "danger" if impact.outcome == "invalid" else "prepared"
            cells[target_field] = MappingReviewCell(
                value=(
                    "Not produced"
                    if impact.outcome == "invalid"
                    else _display_reference_value(value)
                ),
                style=style,
                comment=_impact_comment(impact),
            )

        issues = tuple(row.issues)
        if any(issue.severity == "error" for issue in issues):
            status, status_style = "Must fix", "danger"
        elif issues:
            status, status_style = "Review required", "warning"
        else:
            status, status_style = "Ready", "ready"
        rows.append(
            MappingReviewDataRow(
                source_table=row.dataset,
                source_row=_source_row_reference(row, physical_labels),
                target_model=row.target_model,
                status=status,
                status_style=status_style,
                cells=cells,
            )
        )
    return MappingReviewRowProjection(
        rows=tuple(rows),
        changed_cell_count=changed_count,
    )


def _identity_values(components, values) -> dict[str, Any]:
    result: dict[str, Any] = {}
    cursor = 0
    for component in components:
        width = len(component.target_fields)
        prepared = tuple(values[cursor : cursor + width])
        cursor += width
        for target_field, value in zip(
            component.target_fields,
            prepared,
            strict=True,
        ):
            result[target_field] = _display_reference_value(value)
    return result


def _source_row_reference(row, physical_labels: dict[str, str]) -> str:
    sources = dict(row.lineage.physical_sources)
    if len(sources) == 1:
        dataset_id, source_rows = next(iter(sources.items()))
        if len(source_rows) == 1:
            return str(source_rows[0])
        return ", ".join(str(value) for value in source_rows)
    return "; ".join(
        f"{physical_labels.get(dataset_id, dataset_id)}: "
        f"{', '.join(str(value) for value in source_rows)}"
        for dataset_id, source_rows in sorted(sources.items())
    )


def _impact_comment(impact: TransformationImpactRow) -> str:
    lines = [
        f"Source field: {impact.source_column}",
        f"Original value: {impact.raw_value}",
        f"Rule: {impact.rules}",
    ]
    if impact.message:
        lines.append(f"Result: {impact.message}")
    return "\n".join(lines)


def _display_reference_value(value: Any) -> Any:
    if isinstance(value, LogicalReference | BusinessReference):
        key = " / ".join(_display_text(item) for item in value.key)
        scope = " / ".join(_display_text(item) for item in value.scope)
        return f"{key} [{scope}]" if scope else key
    if isinstance(value, tuple | list):
        return " / ".join(_display_text(item) for item in value)
    return value


def _display_text(value: Any) -> str:
    portable = portable_value(value)
    if isinstance(portable, dict) and "value" in portable:
        return str(portable["value"])
    if portable is None:
        return ""
    return str(portable)


def write_mapping_review_workbook(
    revision: MappingRevision,
    validation: MappingValidationResult,
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    workbook_path: str | Path,
    *,
    row_projection: MappingReviewRowProjection | None = None,
    row_projection_error: str = "",
    recipe_context: MappingReviewRecipeContext | None = None,
) -> Path:
    """Write one read-only matching review from exact checked evidence."""

    _require_bound_evidence(revision, validation, selection)
    target = Path(workbook_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    overview = workbook.active
    overview.title = "Matching overview"
    attention = workbook.create_sheet("Needs attention")
    field_matches = workbook.create_sheet("Field matches")
    transformed_data = workbook.create_sheet("Transformed data")
    value_coverage = workbook.create_sheet("Value coverage")

    source_by_id = {item.dataset_id: item for item in selection.datasets}
    models_by_name = {item.name: item for item in schema.models}
    issues_by_field: dict[tuple[str, str], list[MappingValidationIssue]] = (
        defaultdict(list)
    )
    for issue in validation.issues:
        if issue.dataset_id and issue.target_field:
            issues_by_field[(issue.dataset_id, issue.target_field)].append(issue)

    field_reviews: list[_FieldReview] = []
    for dataset in revision.definition.datasets:
        source_dataset = source_by_id.get(dataset.dataset_id)
        model = models_by_name.get(dataset.target_model)
        if source_dataset is None or model is None:
            continue
        reviews = _dataset_field_reviews(
            dataset,
            source_dataset,
            model.fields,
            issues_by_field,
        )
        field_reviews.extend(reviews)

    _write_overview(
        overview,
        revision,
        validation,
        selection,
        schema,
        field_reviews,
        row_projection,
        recipe_context,
    )
    _write_attention(attention, validation, source_by_id, models_by_name)
    _write_field_matches(field_matches, field_reviews)
    _write_transformed_data(
        transformed_data,
        field_reviews,
        row_projection,
        row_projection_error,
    )
    _write_value_coverage(
        value_coverage,
        revision,
        validation,
        source_by_id,
        models_by_name,
        recipe_context,
    )

    workbook.calculation.fullCalcOnLoad = False
    workbook.calculation.forceFullCalc = False
    workbook.calculation.calcMode = "manual"
    temporary = target.with_name(f".{target.name}.partial")
    try:
        workbook.save(temporary)
        temporary.replace(target)
    except (OSError, ValueError) as error:
        raise MappingReviewGenerationError(
            "Matching review workbook generation failed"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)
        workbook.close()
    return target


def _require_bound_evidence(
    revision: MappingRevision,
    validation: MappingValidationResult,
    selection: SourceSelection,
) -> None:
    definition = revision.definition
    if validation.mapping_content_hash != definition.content_hash:
        raise MappingReviewGenerationError(
            "Check matches again before creating the workbook"
        )
    if (
        validation.source_selection_hash != selection.content_hash
        or definition.source_selection_hash != selection.content_hash
    ):
        raise MappingReviewGenerationError(
            "The checked source data is no longer current"
        )
    if validation.schema_hash != definition.schema_hash:
        raise MappingReviewGenerationError(
            "The checked Odoo fields are no longer current"
        )


def _dataset_field_reviews(
    dataset: DatasetMapping,
    source_dataset: SourceDataset,
    schema_fields: tuple[SchemaField, ...],
    issues_by_field: dict[tuple[str, str], list[MappingValidationIssue]],
) -> tuple[_FieldReview, ...]:
    fields_by_name = {item.name: item for item in schema_fields}
    scalar_by_field = {item.target_field: item for item in dataset.fields}
    relation_by_field = {
        item.target_field: item for item in dataset.relationships
    }
    disposition_by_field = {
        item.target_field: item for item in dataset.target_field_dispositions
    }
    identity_fields = {
        target_field
        for component in (*dataset.target_identity, *dataset.target_scope)
        for target_field in component.target_fields
    }
    relevant = set(scalar_by_field) | set(relation_by_field) | set(
        disposition_by_field
    ) | identity_fields
    relevant.update(
        item.name
        for item in schema_fields
        if item.required
        and not item.readonly
        and dataset.mode is not MappingTargetMode.REFERENCE
    )
    relevant.update(
        target_field
        for issue_dataset, target_field in issues_by_field
        if issue_dataset == dataset.dataset_id
    )
    source_labels = {
        item.stable_key: item.source_name for item in source_dataset.columns
    }

    reviews: list[_FieldReview] = []
    for field_name in sorted(
        relevant,
        key=lambda name: (
            fields_by_name.get(name).label.casefold()
            if fields_by_name.get(name) is not None
            else name.casefold()
        ),
    ):
        field = fields_by_name.get(field_name)
        if field is None:
            continue
        issues = issues_by_field.get((dataset.dataset_id, field_name), ())
        scalar = scalar_by_field.get(field_name)
        relation = relation_by_field.get(field_name)
        disposition = disposition_by_field.get(field_name)
        status, style, next_action = _field_status(
            field,
            issues,
            scalar,
            relation,
            disposition.handling if disposition is not None else None,
            field_name in identity_fields,
        )
        provider, source_fields = _field_provider(
            scalar,
            relation,
            disposition.handling if disposition is not None else None,
            field_name in identity_fields,
            source_labels,
        )
        reviews.append(
            _FieldReview(
                dataset=dataset,
                source_dataset=source_dataset,
                field=field,
                status=status,
                style=style,
                provider=provider,
                source_fields=source_fields,
                requirement=(
                    "Required by Odoo"
                    if field.required and not field.readonly
                    else "Optional or managed by Odoo"
                ),
                next_action=next_action,
            )
        )
    return tuple(reviews)


def _field_status(
    field: SchemaField,
    issues: Iterable[MappingValidationIssue],
    scalar: ScalarFieldMapping | None,
    relation: RelationshipMapping | None,
    handling: TargetFieldHandling | None,
    identity: bool,
) -> tuple[str, str, str]:
    issues = tuple(issues)
    errors = tuple(item for item in issues if item.severity == "error")
    warnings = tuple(item for item in issues if item.severity == "warning")
    if errors:
        return "Must fix", "danger", errors[0].remediation
    if warnings:
        return "Review required", "warning", warnings[0].remediation
    if handling is TargetFieldHandling.ODOO_DEFAULT:
        return (
            "Odoo will choose",
            "warning",
            "Review the verified Odoo default before confirming the matches.",
        )
    if handling is TargetFieldHandling.ODOO_MANAGED:
        return (
            "Odoo manages this field",
            "warning",
            "Review that Odoo creates or maintains this field.",
        )
    if relation is not None:
        return (
            "Relationship configured",
            "ready",
            "Preparation will check every related key for one exact record.",
        )
    if scalar is not None:
        if scalar.value_source is ScalarValueSource.ODOO_DEFAULT:
            return (
                "Odoo will choose",
                "warning",
                "Review the verified Odoo default before confirming the matches.",
            )
        if (
            scalar.value_source is not ScalarValueSource.SOURCE
            or scalar.transform != ScalarTransformPolicy()
        ):
            return (
                "Impodo supplies or prepares",
                "prepared",
                "Review the configured value or rule effect.",
            )
        return "Mapped", "ready", "No mapping correction is currently required."
    if identity:
        return "Identity configured", "ready", "No mapping correction is currently required."
    if field.required and not field.readonly:
        return (
            "Must fix",
            "danger",
            "Map incoming data, provide a fixed value, or confirm an available Odoo default.",
        )
    return "Not used", "neutral", "No action is currently required."


def _field_provider(
    scalar: ScalarFieldMapping | None,
    relation: RelationshipMapping | None,
    handling: TargetFieldHandling | None,
    identity: bool,
    source_labels: dict[str, str],
) -> tuple[str, str]:
    if handling is TargetFieldHandling.ODOO_DEFAULT:
        return "Verified Odoo default", ""
    if handling is TargetFieldHandling.ODOO_MANAGED:
        return "Odoo-managed field", ""
    if relation is not None:
        if relation.value_source is RelationshipValueSource.CONSTANT_EXISTING:
            reference = relation.constant_reference
            display = " · ".join(
                f"{item.target_field}={item.value}"
                for item in (
                    *((reference.key_values if reference is not None else ())),
                    *((reference.scope_values if reference is not None else ())),
                )
            )
            return "Same existing Odoo record for every row", display
        source = ", ".join(
            source_labels.get(item, item) for item in relation.source_column_keys
        )
        return f"{relation.kind} relationship", source
    if scalar is not None:
        source = (
            " + ".join(
                source_labels.get(key, key)
                for key in scalar.concatenation.source_column_keys
            )
            if scalar.concatenation is not None
            else (
                source_labels.get(
                    scalar.source_column_key,
                    scalar.source_column_key,
                )
                if scalar.source_column_key
                else ""
            )
        )
        provider = {
            ScalarValueSource.SOURCE: "Incoming value",
            ScalarValueSource.CONSTANT: "Fixed value",
            ScalarValueSource.SOURCE_WITH_FALLBACK: "Incoming value with backup",
            ScalarValueSource.CONCATENATE: "Combined source columns",
            ScalarValueSource.CONDITIONAL_RULES: "Ordered choice rules",
            ScalarValueSource.ODOO_DEFAULT: "Odoo default",
        }[scalar.value_source]
        return provider, source
    if identity:
        return "Business identity", "See the confirmed identity mapping"
    return "No value provider", ""


def _write_overview(
    sheet,
    revision: MappingRevision,
    validation: MappingValidationResult,
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    field_reviews: list[_FieldReview],
    row_projection: MappingReviewRowProjection | None,
    recipe_context: MappingReviewRecipeContext | None,
) -> None:
    _title_band(
        sheet,
        "Impodo matching review",
        "Stage 3 checked matches — not final load readiness",
        8,
    )
    error_count = sum(item.severity == "error" for item in validation.issues)
    warning_count = sum(item.severity == "warning" for item in validation.issues)
    if validation.status is MappingValidationStatus.INVALID:
        status, style, action = (
            "Cannot confirm matches",
            "danger",
            "Open Needs attention and fix every red item in Impodo.",
        )
    elif warning_count:
        status, style, action = (
            "Review required",
            "warning",
            "Review every amber item before confirming the matches.",
        )
    else:
        status, style, action = (
            "Ready to confirm",
            "ready",
            "Review the field matches, then confirm them in Impodo.",
        )
    coverage = validation.categorical_coverage
    recipe_gap_count = (
        sum(
            max(len(result.uncovered_values), 1)
            for result in coverage.field_results
            if result.status in {"UNCOVERED", "UNSUPPORTED"}
        )
        if recipe_context is not None and coverage is not None
        else 0
    )
    rows = [
        ("Check result", status),
        ("Must fix", error_count),
        ("Review items", warning_count),
        ("Tables checked", len(revision.definition.datasets)),
        ("Fields shown", len(field_reviews)),
        (
            "Rows shown",
            len(row_projection.rows) if row_projection is not None else 0,
        ),
        (
            "Changed values",
            row_projection.changed_cell_count
            if row_projection is not None
            else 0,
        ),
        (
            "Rule origin",
            recipe_context.label
            if recipe_context is not None
            else "Current Stage 3 mapping",
        ),
    ]
    if recipe_context is not None:
        rows.append(("Recipe coverage gaps", recipe_gap_count))
    rows.extend(
        (
            ("Next action", action),
            ("Odoo target", f"{schema.database} — Odoo {schema.odoo_version}"),
            ("Checked mapping", f"Version {revision.version}"),
        )
    )
    _write_key_values(sheet, rows, start_row=4)
    _style_status_cell(sheet["B4"], style)

    sheet["D4"] = "How to use this workbook"
    sheet["D4"].fill = PatternFill("solid", fgColor=_COLORS["brand"])
    sheet["D4"].font = Font(bold=True, color=_COLORS["white"])
    sheet.merge_cells("D4:H4")
    sheet["D5"] = (
        "Start with Needs attention. Field matches lists each decision once. "
        "Transformed data shows proposed rows with blue changed cells and "
        "notes containing the original values. Value coverage groups current "
        "source choices. Correct data or rules in Impodo, then run Check "
        "matches and recreate this workbook."
    )
    sheet["D5"].alignment = Alignment(vertical="top", wrap_text=True)
    sheet["D5"].fill = PatternFill("solid", fgColor=_COLORS["paper"])
    sheet.merge_cells("D5:H10")
    sheet.row_dimensions[5].height = 88

    legend = (
        ("Must fix", "The checked mapping cannot be confirmed.", "danger"),
        ("Review required", "A deliberate decision needs review.", "warning"),
        ("Mapped", "The field mapping is currently valid.", "ready"),
        ("Impodo prepares", "Impodo supplies or transforms the value.", "prepared"),
        ("Not used", "No action is currently required.", "neutral"),
    )
    sheet["A18"] = "Colour and status meanings"
    sheet.merge_cells("A18:H18")
    _style_header(sheet, 18, 1, 8, _COLORS["charcoal_dark"])
    for row_index, (label, meaning, legend_style) in enumerate(legend, start=19):
        sheet.cell(row_index, 1, label)
        sheet.cell(row_index, 2, meaning)
        sheet.merge_cells(
            start_row=row_index,
            start_column=2,
            end_row=row_index,
            end_column=8,
        )
        _style_status_cell(sheet.cell(row_index, 1), legend_style)
    sheet["A26"] = "What this workbook does not contain"
    sheet.merge_cells("A26:H26")
    _style_header(sheet, 26, 1, 8, _COLORS["charcoal_dark"])
    sheet["A27"] = (
        "Transformed data is a Stage 3 preview. Stage 4 still publishes prepared "
        "row evidence and resolves duplicates and related records. Stage 5 still "
        "compares those rows with fresh Odoo evidence. Use the separate Stage 5 "
        "review workbook for the proposed load."
    )
    sheet.merge_cells("A27:H29")
    sheet["A27"].alignment = Alignment(vertical="top", wrap_text=True)
    sheet["A27"].fill = PatternFill("solid", fgColor=_COLORS["soft"])
    sheet.freeze_panes = "A4"
    sheet.column_dimensions["A"].width = 24
    sheet.column_dimensions["B"].width = 24
    for column in "CDEFGH":
        sheet.column_dimensions[column].width = 18


def _write_attention(sheet, validation, source_by_id, models_by_name) -> None:
    headers = (
        "Status",
        "Source table",
        "Odoo model",
        "Odoo field",
        "Source field",
        "Affected rows",
        "Problem",
        "How to fix",
    )
    rows = []
    for issue in sorted(
        validation.issues,
        key=lambda item: (
            0 if item.severity == "error" else 1,
            item.dataset_id or "",
            item.target_field or "",
            item.code,
        ),
    ):
        source = source_by_id.get(issue.dataset_id or "")
        model = models_by_name.get(issue.target_model or "")
        fields = {item.name: item for item in model.fields} if model else {}
        field = fields.get(issue.target_field or "")
        source_fields = (
            {item.stable_key: item.source_name for item in source.columns}
            if source
            else {}
        )
        rows.append(
            (
                "Must fix" if issue.severity == "error" else "Review",
                source.name if source else issue.dataset_id or "All tables",
                model.label if model else issue.target_model or "",
                field.label if field else issue.target_field or "",
                source_fields.get(
                    issue.source_column_key or "",
                    issue.source_column_key or "",
                ),
                _issue_affected_rows(validation, issue),
                issue.message,
                issue.remediation,
            )
        )
    if not rows:
        rows.append(
            (
                "Ready",
                "",
                "",
                "",
                "",
                "",
                "No current findings",
                "No action required",
            )
        )
    _write_table_sheet(
        sheet,
        "Needs attention",
        "Fix red items and review amber items in Impodo",
        headers,
        rows,
    )
    for row_index in range(4, sheet.max_row + 1):
        status = str(sheet.cell(row_index, 1).value or "")
        style = "danger" if status == "Must fix" else "warning" if status == "Review" else "ready"
        _style_status_cell(sheet.cell(row_index, 1), style)
        if sheet.cell(row_index, 4).value:
            _style_status_cell(sheet.cell(row_index, 4), style)


def _write_field_matches(sheet, reviews: list[_FieldReview]) -> None:
    headers = (
        "Source table",
        "Odoo model",
        "Odoo field",
        "Technical field",
        "Type",
        "Requirement",
        "Value source",
        "Source field",
        "Status",
        "Next action",
    )
    rows = [
        (
            item.source_dataset.name,
            item.dataset.target_model,
            item.field.label,
            item.field.name,
            item.field.type,
            item.requirement,
            item.provider,
            item.source_fields,
            item.status,
            item.next_action,
        )
        for item in reviews
    ]
    _write_table_sheet(
        sheet,
        "Field matches",
        "One row for each mapped, required, or checked Odoo field",
        headers,
        rows,
    )
    for row_index, review in enumerate(reviews, start=4):
        _style_status_cell(sheet.cell(row_index, 3), review.style)
        _style_status_cell(sheet.cell(row_index, 9), review.style)


def _issue_affected_rows(validation, issue) -> int | str:
    evidence = validation.categorical_coverage
    if evidence is None or not issue.dataset_id or not issue.target_field:
        return ""
    result = next(
        (
            item
            for item in evidence.field_results
            if item.dataset_id == issue.dataset_id
            and item.target_field == issue.target_field
        ),
        None,
    )
    if result is None or not result.uncovered_values:
        return ""
    counts = {item.values: item.count for item in result.distinct_values}
    return sum(counts.get(values, 0) for values in result.uncovered_values)


def _write_transformed_data(
    sheet,
    reviews: list[_FieldReview],
    row_projection: MappingReviewRowProjection | None,
    row_projection_error: str,
) -> None:
    if row_projection is None:
        _write_table_sheet(
            sheet,
            "Transformed data",
            "Stage 3 proposed values with original values in changed-cell notes",
            ("Source table", "Source row", "Odoo model", "Row status", "Details"),
            (
                (
                    "",
                    "",
                    "",
                    "Not available",
                    row_projection_error
                    or "No file-source row preview is available for this check.",
                ),
            ),
        )
        _style_status_cell(sheet["D4"], "neutral")
        return

    column_reviews: dict[tuple[str, str], _FieldReview] = {}
    for review in reviews:
        column_reviews.setdefault(
            (review.dataset.target_model, review.field.name),
            review,
        )
    columns = tuple(column_reviews)
    headers = (
        "Source table",
        "Source row",
        "Odoo model",
        "Row status",
        *(
            f"{model} · {column_reviews[(model, field)].field.label}"
            for model, field in columns
        ),
    )
    if len(headers) > _EXCEL_MAX_COLUMN:
        raise MappingReviewGenerationError(
            "Transformed data has too many Odoo fields for Excel"
        )
    rows: list[tuple[Any, ...]] = []
    cell_styles: dict[tuple[int, int], str] = {}
    cell_comments: dict[tuple[int, int], str] = {}
    for row_offset, item in enumerate(row_projection.rows, start=4):
        row_status = item.status
        row_status_style = item.status_style
        values: list[Any] = [
            item.source_table,
            item.source_row,
            item.target_model,
            row_status,
        ]
        for column_index, (model, target_field) in enumerate(columns, start=5):
            review = column_reviews[(model, target_field)]
            if model != item.target_model:
                values.append("")
                continue
            projected = item.cells.get(target_field)
            if projected is None:
                if review.style == "danger":
                    values.append("Not produced")
                    cell_styles[(row_offset, column_index)] = "danger"
                    row_status = "Must fix"
                    row_status_style = "danger"
                else:
                    values.append("")
                continue
            values.append(projected.value)
            if projected.style != "neutral":
                cell_styles[(row_offset, column_index)] = projected.style
                if projected.style == "danger":
                    row_status = "Must fix"
                    row_status_style = "danger"
            elif review.style == "warning":
                cell_styles[(row_offset, column_index)] = "warning"
                if row_status_style not in {"danger", "warning"}:
                    row_status = "Review required"
                    row_status_style = "warning"
            if projected.comment:
                cell_comments[(row_offset, column_index)] = projected.comment
        values[3] = row_status
        cell_styles[(row_offset, 4)] = row_status_style
        rows.append(tuple(values))
    if not rows:
        rows.append(("", "", "", "No source rows", *("" for _ in columns)))
        cell_styles[(4, 4)] = "neutral"
    _write_table_sheet(
        sheet,
        "Transformed data",
        "Stage 3 proposed values with original values in changed-cell notes",
        headers,
        rows,
    )
    for (row_index, column_index), style in cell_styles.items():
        _style_status_cell(sheet.cell(row_index, column_index), style)
    for (row_index, column_index), comment in cell_comments.items():
        sheet.cell(row_index, column_index).comment = Comment(comment, "Impodo")
    sheet.freeze_panes = "E4"
    for column in range(5, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 24


def _write_value_coverage(
    sheet,
    revision,
    validation,
    source_by_id,
    models_by_name,
    recipe_context,
) -> None:
    headers = (
        "Status",
        "Rule origin",
        "Source table",
        "Odoo field",
        "Source columns",
        "Current source value",
        "Source rows",
        "Proposed Odoo value",
        "Coverage rule",
        "How to fix",
    )
    rows: list[tuple[Any, ...]] = []
    styles: list[str] = []
    mapping_by_id = {
        dataset.dataset_id: dataset for dataset in revision.definition.datasets
    }
    evidence = validation.categorical_coverage
    if evidence is not None:
        for result in evidence.field_results:
            source = source_by_id.get(result.dataset_id)
            mapping = mapping_by_id.get(result.dataset_id)
            model = (
                models_by_name.get(mapping.target_model)
                if mapping is not None
                else None
            )
            fields = {item.name: item for item in model.fields} if model else {}
            field = fields.get(result.target_field)
            labels = (
                {item.stable_key: item.source_name for item in source.columns}
                if source is not None
                else {}
            )
            source_columns = ", ".join(
                labels.get(key, key) for key in result.source_column_keys
            )
            protected_source = bool(
                source is not None and source.origin is SourceOriginKind.ODOO
            )
            origin = (
                recipe_context.label
                if recipe_context is not None
                else "Current Stage 3 mapping"
            )
            if protected_source:
                rows.append(
                    (
                        "Protected",
                        origin,
                        source.name,
                        field.label if field else result.target_field,
                        source_columns,
                        "Protected values remain in Impodo",
                        sum(item.count for item in result.distinct_values),
                        "",
                        result.policy.replace("_", " ").title(),
                        "Review the values in Impodo.",
                    )
                )
                styles.append("neutral")
                continue
            if result.status == "UNSUPPORTED":
                rows.append(
                    (
                        "Could not evaluate",
                        origin,
                        source.name if source else result.dataset_id,
                        field.label if field else result.target_field,
                        source_columns,
                        "",
                        0,
                        "",
                        result.policy.replace("_", " ").title(),
                        "Change the rule so Impodo can prove every current value.",
                    )
                )
                styles.append("danger")
                continue
            uncovered = set(result.uncovered_values)
            listed: set[tuple[str, ...]] = set()
            for value_count in result.distinct_values:
                values = value_count.values
                listed.add(values)
                is_gap = values in uncovered
                status = _coverage_status(recipe_context, is_gap=is_gap)
                rows.append(
                    (
                        status,
                        origin,
                        source.name if source else result.dataset_id,
                        field.label if field else result.target_field,
                        source_columns,
                        " | ".join(values),
                        value_count.count,
                        _coverage_proposed_value(mapping, result, values),
                        result.policy.replace("_", " ").title(),
                        _coverage_action(recipe_context, is_gap=is_gap),
                    )
                )
                styles.append("danger" if is_gap else "ready")
            for values in result.uncovered_values:
                if values in listed:
                    continue
                rows.append(
                    (
                        _coverage_status(recipe_context, is_gap=True),
                        origin,
                        source.name if source else result.dataset_id,
                        field.label if field else result.target_field,
                        source_columns,
                        " | ".join(values),
                        0,
                        _coverage_proposed_value(mapping, result, values),
                        result.policy.replace("_", " ").title(),
                        _coverage_action(recipe_context, is_gap=True),
                    )
                )
                styles.append("danger")
            if not result.distinct_values and not result.uncovered_values:
                rows.append(
                    (
                        _coverage_status(recipe_context, is_gap=False),
                        origin,
                        source.name if source else result.dataset_id,
                        field.label if field else result.target_field,
                        source_columns,
                        "No nonblank current values",
                        0,
                        _coverage_proposed_value(mapping, result, ()),
                        result.policy.replace("_", " ").title(),
                        _coverage_action(recipe_context, is_gap=False),
                    )
                )
                styles.append("ready")
    if not rows:
        rows.append(("Not applicable", "", "", "", "", "", 0, "", "", ""))
        styles.append("neutral")
    subtitle = (
        f"Current source choices checked against {recipe_context.label}"
        if recipe_context is not None
        else "Current source choices checked against Odoo choices or business keys"
    )
    _write_table_sheet(sheet, "Value coverage", subtitle, headers, rows)
    for row_index, style in enumerate(styles, start=4):
        _style_status_cell(sheet.cell(row_index, 1), style)
        if style == "danger":
            _style_status_cell(sheet.cell(row_index, 6), style)


def _coverage_status(recipe_context, *, is_gap: bool) -> str:
    if recipe_context is not None:
        return "Not covered by Recipe" if is_gap else "Covered by Recipe"
    return "Must fix" if is_gap else "Covered"


def _coverage_action(recipe_context, *, is_gap: bool) -> str:
    if not is_gap:
        return "No action is required."
    if recipe_context is not None:
        return "Match this current value in the Recipe-based rules and check again."
    return "Match this current value in Impodo and check again."


def _coverage_proposed_value(mapping, result, values: tuple[str, ...]) -> Any:
    if mapping is None:
        return ""
    scalar = next(
        (
            field
            for field in mapping.fields
            if field.target_field == result.target_field
        ),
        None,
    )
    if scalar is not None:
        if values and values[0].startswith("<fallback:"):
            return values[0].removeprefix("<fallback:").removesuffix(">")
        raw = values[0] if len(values) == 1 else None
        try:
            proposed = evaluate_scalar_mapping_value(
                scalar,
                raw,
                source_values_by_key=dict(
                    zip(result.source_column_keys, values, strict=True)
                )
                if len(result.source_column_keys) == len(values)
                else {},
            )
        except (ScalarValueError, ValueError):
            return "Not produced"
        return "Blank" if proposed is None else _display_reference_value(proposed)
    relationship = next(
        (
            item
            for item in mapping.relationships
            if item.target_field == result.target_field
        ),
        None,
    )
    if relationship is None:
        return ""
    if relationship.value_source is RelationshipValueSource.CONSTANT_EXISTING:
        reference = relationship.constant_reference
        if reference is None:
            return "Not produced"
        return " / ".join(
            str(item.value)
            for item in (*reference.key_values, *reference.scope_values)
        )
    if len(values) == 1:
        matched = next(
            (
                item.target_value
                for item in relationship.resolver.value_mappings
                if item.source_value == values[0]
            ),
            None,
        )
        if matched is not None:
            return matched
    return " / ".join(values)


def _write_table_sheet(sheet, title, subtitle, headers, rows) -> None:
    if len(rows) > _EXCEL_MAX_ROW - 3:
        raise MappingReviewGenerationError(f"{title} exceeds the Excel row limit")
    if len(headers) > _EXCEL_MAX_COLUMN:
        raise MappingReviewGenerationError(f"{title} exceeds the Excel column limit")
    _title_band(sheet, title, subtitle, len(headers))
    for column_index, header in enumerate(headers, start=1):
        sheet.cell(3, column_index, header)
    _style_header(sheet, 3, 1, len(headers), _COLORS["charcoal_dark"])
    for row_index, row in enumerate(rows, start=4):
        for column_index, value in enumerate(row, start=1):
            cell = sheet.cell(row_index, column_index, _safe_cell(value))
            cell.fill = PatternFill(
                "solid",
                fgColor=(
                    _COLORS["surface"]
                    if row_index % 2 == 0
                    else _COLORS["paper"]
                ),
            )
            cell.font = Font(color=_COLORS["charcoal"])
            cell.border = _THIN_BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.auto_filter.ref = f"A3:{get_column_letter(len(headers))}{max(sheet.max_row, 3)}"
    sheet.freeze_panes = "A4"
    sheet.sheet_view.showGridLines = False
    for column_index, header in enumerate(headers, start=1):
        width = 18
        if header in {"Problem", "How to fix", "Next action", "Details"}:
            width = 42
        elif header in {
            "Source table",
            "Odoo model",
            "Odoo field",
            "Source field",
            "Value source",
            "Current source value",
            "Proposed Odoo value",
        }:
            width = 26
        elif header in {"Source row", "Source rows", "Affected rows"}:
            width = 14
        sheet.column_dimensions[get_column_letter(column_index)].width = width


def _write_key_values(sheet, rows, *, start_row) -> None:
    for row_index, (label, value) in enumerate(rows, start=start_row):
        sheet.cell(row_index, 1, label)
        sheet.cell(row_index, 2, _safe_cell(value))
        sheet.cell(row_index, 1).fill = PatternFill("solid", fgColor=_COLORS["soft"])
        sheet.cell(row_index, 1).font = Font(bold=True, color=_COLORS["charcoal_dark"])
        sheet.cell(row_index, 2).font = Font(color=_COLORS["charcoal"])
        sheet.cell(row_index, 1).border = _THIN_BORDER
        sheet.cell(row_index, 2).border = _THIN_BORDER
        sheet.cell(row_index, 2).alignment = Alignment(wrap_text=True)


def _title_band(sheet, title: str, subtitle: str, width: int) -> None:
    width = max(width, 1)
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    sheet["A1"] = title
    sheet["A1"].fill = PatternFill("solid", fgColor=_COLORS["charcoal_dark"])
    sheet["A1"].font = Font(size=16, bold=True, color=_COLORS["white"])
    sheet["A1"].alignment = Alignment(vertical="center")
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
    sheet["A2"] = subtitle
    sheet["A2"].fill = PatternFill("solid", fgColor=_COLORS["paper"])
    sheet["A2"].font = Font(color=_COLORS["charcoal"])
    sheet["A2"].alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 30
    sheet.row_dimensions[2].height = 28


def _style_header(sheet, row: int, start: int, end: int, color: str) -> None:
    for column in range(start, end + 1):
        cell = sheet.cell(row, column)
        cell.fill = PatternFill("solid", fgColor=color)
        cell.font = Font(bold=True, color=_COLORS["white"])
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[row].height = 30


def _style_status_cell(cell, style: str) -> None:
    fill, font = _STYLE_COLORS[style]
    cell.fill = PatternFill("solid", fgColor=fill)
    cell.font = Font(bold=True, color=font)
    cell.alignment = Alignment(vertical="top", wrap_text=True)


def _safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        value = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    if isinstance(value, str) and value.lstrip("\t\r\n").startswith(
        ("=", "+", "-", "@")
    ):
        return f"'{value}"
    return value
