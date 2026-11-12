"""Write a complete data-assistant correction workbook from saved findings.

Generation is read-only and returns completed bytes atomically. Worksheet and
cell limits are checked explicitly; source text never becomes an Excel formula.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Protocol, cast

from openpyxl import Workbook
from openpyxl.cell.cell import Cell, WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._write_only import WriteOnlyWorksheet
from openpyxl.worksheet._writer import WorksheetWriter
from openpyxl.worksheet.dimensions import DimensionHolder
from openpyxl.worksheet.filters import AutoFilter

from impodo.domain.preflight.attention import AttentionExport, AttentionRecord

from .reporting import ReportGenerationError

_CHARCOAL = "494D46"
_PAPER = "F4F4F1"
_DANGER = "FCE8E7"
_WARNING = "FFF5DF"
_MAX_ROWS = 1_048_573  # Three heading rows.
_MAX_COLUMNS = 16_384
_WRAPPED = Alignment(vertical="top", wrap_text=True)
_ALTERNATE_FILL = PatternFill("solid", fgColor=_PAPER)
_ERROR_FILL = PatternFill("solid", fgColor=_DANGER)
_REVIEW_FILL = PatternFill("solid", fgColor=_WARNING)
_HEADING_FILL = PatternFill("solid", fgColor=_CHARCOAL)
_HEADING_FONT = Font(color="FFFFFF", bold=True)
_LINK_FONT = Font(color="2F628F", underline="single")


class _StreamingState(Protocol):
    """Type the attributes initialized by openpyxl's delegated worksheet setup."""

    column_dimensions: DimensionHolder
    auto_filter: AutoFilter
    _writer: WorksheetWriter | None


def _state(sheet: WriteOnlyWorksheet) -> _StreamingState:
    # WriteOnlyWorksheet delegates _setup to Worksheet and creates its writer
    # on append. The library's worksheet interface omits those runtime fields.
    return cast(_StreamingState, sheet)


@dataclass(slots=True)
class _Sheet:
    """Track worksheet coordinates without retaining written cells in memory."""

    worksheet: WriteOnlyWorksheet
    columns: int
    row: int = 3

    @property
    def title(self) -> str:
        return self.worksheet.title


def _text(cell: Cell, value: object) -> None:
    """Preserve literal text, including formula-like strings and leading zeros."""

    text = "" if value is None else str(value)
    text = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f]", lambda match: f"\\u{ord(match[0]):04x}", text
    )
    if len(text) > 32_767:
        raise ReportGenerationError(
            "A report cell exceeds Excel's text limit. Review this finding in Impodo before exporting."
        )
    cell.value = text
    cell.data_type = "s"
    cell.alignment = _WRAPPED


def _sheet(workbook: Workbook, title: str, headers: Sequence[str]) -> _Sheet:
    """Create readable headings and stable, unique Excel-safe worksheet names."""

    if len(headers) > _MAX_COLUMNS:
        raise ReportGenerationError(
            "The affected table exceeds Excel's column limit. Reduce the selected columns before exporting."
        )
    base = re.sub(r"[\[\]:*?/\\]", " ", title).strip(" '") or "Affected data"
    name = base[:31]
    index = 2
    while name.casefold() in {item.casefold() for item in workbook.sheetnames}:
        suffix = f" {index}"
        name = base[: 31 - len(suffix)] + suffix
        index += 1
    sheet = workbook.create_sheet(name)
    if not isinstance(sheet, WriteOnlyWorksheet):
        raise ReportGenerationError("The correction workbook needs a streaming writer.")
    sheet.freeze_panes = "D4"
    sheet.sheet_view.showGridLines = False
    cells = []
    for column, label in enumerate(headers, 1):
        cell = WriteOnlyCell(sheet)
        _text(cell, label)
        cell.fill = _HEADING_FILL
        cell.font = _HEADING_FONT
        cells.append(cell)
        _state(sheet).column_dimensions[get_column_letter(column)].width = (
            48
            if label
            in {
                "Why this needs attention",
                "Suggested action",
                "Source location",
                "Related rows",
            }
            else 28
        )
    heading = WriteOnlyCell(sheet)
    _text(heading, title)
    heading.font = Font(size=16, bold=True, color=_CHARCOAL)
    sheet.append([heading])
    sheet.append([])
    sheet.append(cells)
    return _Sheet(sheet, len(headers))


def _append(
    sheet: _Sheet,
    values: Sequence[object],
    *,
    hyperlinks: Mapping[int, str] | None = None,
    highlights: set[int] | None = None,
    priority: str = "",
) -> int:
    row = sheet.row + 1
    cells = []
    for column, value in enumerate(values, 1):
        cell = WriteOnlyCell(sheet.worksheet)
        _text(cell, value)
        if row % 2 == 0:
            cell.fill = _ALTERNATE_FILL
        if highlights and column in highlights:
            cell.fill = _ERROR_FILL
        if column == 1 and priority:
            cell.fill = _ERROR_FILL if priority == "Must fix" else _REVIEW_FILL
        if hyperlinks and column in hyperlinks:
            cell.hyperlink = hyperlinks[column]
            cell.font = _LINK_FONT
        cells.append(cell)
    sheet.worksheet.append(cells)
    sheet.row = row
    return sheet.row


def _finish(sheet: _Sheet) -> None:
    _state(
        sheet.worksheet
    ).auto_filter.ref = f"A3:{get_column_letter(sheet.columns)}{sheet.row}"


def source_location(export: AttentionExport, record: AttentionRecord) -> str:
    """Name every retained contributing physical row without inventing coordinates."""

    return (
        "; ".join(
            f"{export.source_names.get(dataset, 'Source table')} · source rows {', '.join(map(str, rows))}"
            for dataset, rows in sorted(record.locations.items())
        )
        or f"{record.dataset} · dataset row {record.source_row} (physical location not retained)"
    )


def _duplicate_groups(
    workbook: Workbook, export: AttentionExport, by_id: Mapping[str, AttentionRecord]
) -> dict[str, tuple[str, int]]:
    """Write each duplicate peer once per group, including very large groups."""
    groups = {
        finding.group_id: finding.related_rows
        for finding in export.findings
        if finding.group_id
    }
    if not groups:
        return {}
    headers = ("Group", "Table", "Record", "Dataset row", "Source location")
    sheet = _sheet(workbook, "Duplicate groups", headers)
    links = {}
    ordinal = 0
    for group, members in sorted(groups.items()):
        for row_id in members:
            if ordinal and ordinal % _MAX_ROWS == 0:
                _finish(sheet)
                sheet = _sheet(workbook, "Duplicate groups", headers)
            record = by_id.get(row_id)
            if record is None:
                raise ReportGenerationError(
                    "A duplicate group's affected records could not be verified. Prepare the data again."
                )
            row = _append(
                sheet,
                (
                    group,
                    record.dataset,
                    record.record,
                    record.source_row,
                    source_location(export, record),
                ),
            )
            links.setdefault(group, (sheet.title, row))
            ordinal += 1
    _finish(sheet)
    return links


def build_attention_workbook(
    export: AttentionExport,
    source_values: Mapping[tuple[str, int], Mapping[str, str | None]],
) -> bytes:
    """Include all findings and affected data, splitting oversized row sets."""

    workbook = Workbook(write_only=True)
    try:
        return _write_attention_workbook(workbook, export, source_values)
    finally:
        # A failed cell or limit check must also release streaming XML files.
        for sheet in workbook.worksheets:
            if isinstance(sheet, WriteOnlyWorksheet):
                if not sheet.closed:
                    sheet.close()
                writer = _state(sheet)._writer
                if (
                    writer is not None
                    and isinstance(writer.out, str)
                    and Path(writer.out).exists()
                ):
                    writer.cleanup()
        workbook.close()


def _write_attention_workbook(
    workbook: Workbook,
    export: AttentionExport,
    source_values: Mapping[tuple[str, int], Mapping[str, str | None]],
) -> bytes:
    """Stream each complete sheet while retaining only evidence and row indexes."""

    overview = _sheet(workbook, "Overview", ("Report item", "Value"))
    direct = {
        item.row_id
        for item in export.findings
        if item.row_id and item.effect != "Held back by a linked record"
    }
    inherited = {
        item.row_id
        for item in export.findings
        if item.effect == "Held back by a linked record"
    }
    physical = {
        (dataset, row)
        for record in export.records
        for dataset, rows in record.locations.items()
        for row in rows
    }
    coverage = (
        "Odoo comparison completed"
        if export.comparison_at
        else "Odoo comparison not completed"
    )
    metadata = (
        ("Project", export.project),
        ("Data version", export.data_version_id),
        ("Company", export.company),
        ("Report created (UTC)", datetime.now(UTC).isoformat()),
        ("Preparation checked at", export.prepared_at.isoformat()),
        ("Odoo check", coverage),
        (
            "Odoo compared at",
            export.comparison_at.isoformat()
            if export.comparison_at
            else "Not completed",
        ),
        (
            "Affected prepared records",
            export.affected_record_count,
        ),
        ("Findings", len(export.findings)),
        ("Records with their own problem", len(direct)),
        ("Records held back by a linked record", len(inherited)),
        ("Contributing source rows", len(physical)),
        ("Intentionally filtered records", export.filtered_count),
        (
            "How to correct",
            "Start with Needs attention. Correct the source, saved Impodo choice, or Odoo record at the location shown. Then prepare and compare again. Workbook edits do not change Impodo.",
        ),
        (
            "Coverage",
            "Protected Odoo-source values remain inside Impodo."
            if export.restricted
            else "Original file values appear where a verified source snapshot is retained. Not retained means unavailable evidence; (empty) means an actual empty cell.",
        ),
        ("Preparation reference", export.quality_run_id),
        ("Comparison reference", export.comparison_id or "Not completed"),
    )
    for pair in metadata:
        _append(overview, pair)
    for reason, count in sorted(
        Counter(item.problem for item in export.findings).items()
    ):
        _append(overview, (reason, count))
    by_id = {record.row_id: record for record in export.records}
    headers = (
        "Priority",
        "Table",
        "Company",
        "Record",
        "Dataset row",
        "Source location",
        "Column or columns",
        "Source value",
        "Value prepared for Odoo",
        "Problem",
        "Why this needs attention",
        "Related rows",
        "Suggested action",
        "Where to correct",
        "Who can help",
        "Load effect",
        "Group",
        "Support code",
    )
    attention = _sheet(workbook, "Needs attention", headers)
    group_links = _duplicate_groups(workbook, export, by_id)
    links: dict[str, list[tuple[str, int]]] = defaultdict(list)
    affected_fields: dict[str, set[str]] = defaultdict(set)
    for index, finding in enumerate(export.findings):
        if index and index % _MAX_ROWS == 0:
            _finish(attention)
            attention = _sheet(workbook, "Needs attention", headers)
        record = by_id.get(finding.row_id)
        fields = finding.fields
        labels = record.labels if record else {}
        columns = []
        prepared = []
        originals = []
        for field in fields:
            keys = record.field_sources.get(field, (field,)) if record else (field,)
            source_labels = [
                export.source_columns[key]
                for key in keys
                if key in export.source_columns
            ]
            label = labels.get(
                field, export.source_columns.get(field, field.replace("_", " ").title())
            )
            columns.append(
                f"{', '.join(source_labels)} → {label}"
                if source_labels and label not in source_labels
                else label
            )
            prepared.append(
                f"{label}: {record.values.get(field, 'Not retained')}"
                if record
                else "Not retained"
            )
            if record:
                for dataset, rows in record.locations.items():
                    for source_row in rows:
                        source = source_values.get((dataset, source_row), {})
                        originals.extend(
                            f"{export.source_columns.get(key, key)} [row {source_row}]: {'(empty)' if source[key] is None or source[key] == '' else source[key]}"
                            for key in keys
                            if key in source
                        )
        related = (
            "; ".join(
                f"{by_id[row_id].dataset}, dataset row {by_id[row_id].source_row} ({by_id[row_id].record})"
                for row_id in finding.related_rows
                if row_id in by_id
            )
            if not finding.group_id or len(finding.related_rows) <= 10
            else f"{len(finding.related_rows):,} records · see Duplicate groups, group {finding.group_id}"
        )
        group_link = {}
        if finding.group_id in group_links:
            title, position = group_links[finding.group_id]
            group_link[12] = f"#'{title}'!A{position}"
        row = _append(
            attention,
            (
                finding.priority,
                finding.dataset or "Whole load",
                export.company,
                record.record
                if record
                else "Applies to the whole table"
                if finding.dataset
                else "Applies to the whole load",
                record.source_row if record else "",
                source_location(export, record) if record else "",
                "; ".join(columns) or "See affected data",
                "Restricted"
                if export.restricted
                else "; ".join(originals) or "Not retained",
                "; ".join(prepared) or "See affected data",
                finding.problem,
                finding.explanation,
                related or ("Location not retained" if finding.related_rows else ""),
                finding.action,
                finding.correction,
                finding.owner,
                finding.effect,
                finding.group_id,
                finding.code,
            ),
            priority=finding.priority,
            hyperlinks=group_link,
        )
        if record:
            links[record.row_id].append((attention.title, row))
            affected_fields[record.row_id].update(fields)
    if not export.findings:
        _append(
            attention,
            (
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "No saved findings require attention.",
            ),
        )
    _finish(attention)
    by_dataset: dict[str, list[AttentionRecord]] = defaultdict(list)
    for record in export.records:
        by_dataset[record.dataset].append(record)
    for dataset, records in sorted(by_dataset.items()):
        value_fields = sorted({field for record in records for field in record.values})
        physical_columns = sorted(
            {
                (source_id, key)
                for record in records
                for source_id, rows in record.locations.items()
                for source_row in rows
                for key in source_values.get((source_id, source_row), {})
            }
        )
        labels = {
            field: label for record in records for field, label in record.labels.items()
        }
        data_headers = ["Record", "Dataset row", "Source location", "Issues"]
        data_headers.extend(
            f"Prepared · {labels.get(field, field)}" for field in value_fields
        )
        data_headers.extend(
            f"Source · {export.source_names.get(source_id, 'Table')} · {export.source_columns.get(key, key)}"
            for source_id, key in physical_columns
        )
        sheet = _sheet(workbook, f"Data · {dataset}", data_headers)
        first_sheet = sheet
        for index, record in enumerate(records):
            if index and index % _MAX_ROWS == 0:
                _finish(sheet)
                sheet = _sheet(workbook, f"Data · {dataset}", data_headers)
            issue_links = links.get(record.row_id, [])
            raw = []
            for source_id, key in physical_columns:
                cells = [
                    source_values.get((source_id, source_row), {}).get(
                        key, "Not retained"
                    )
                    for source_row in record.locations.get(source_id, ())
                ]
                raw.append(
                    "; ".join(
                        "(empty)" if value is None or value == "" else value
                        for value in cells
                    )
                    or "Not retained"
                )
            record_link = {}
            if issue_links:
                title, position = issue_links[0]
                record_link[4] = f"#'{title.replace(chr(39), chr(39) * 2)}'!A{position}"
            highlights = {
                column
                for column, field in enumerate(value_fields, 5)
                if field in affected_fields[record.row_id]
            }
            source_fields = {
                key
                for field in affected_fields[record.row_id]
                for key in record.field_sources.get(field, (field,))
            }
            highlights.update(
                column
                for column, (_source, key) in enumerate(
                    physical_columns, 5 + len(value_fields)
                )
                if key in source_fields
            )
            _append(
                sheet,
                [
                    record.record,
                    record.source_row,
                    source_location(export, record),
                    "; ".join(
                        f"{title} row {position}" for title, position in issue_links
                    )
                    or "Related record context",
                    *(
                        record.values.get(field, "Not retained")
                        for field in value_fields
                    ),
                    *raw,
                ],
                hyperlinks=record_link,
                highlights=highlights,
            )
        _finish(sheet)
        _append(
            overview,
            (f"Affected data · {dataset}", len(records)),
            hyperlinks={1: f"#'{first_sheet.title.replace(chr(39), chr(39) * 2)}'!A1"},
        )
    _finish(overview)
    stream = BytesIO()
    try:
        workbook.save(stream)
        return stream.getvalue()
    finally:
        workbook.close()
