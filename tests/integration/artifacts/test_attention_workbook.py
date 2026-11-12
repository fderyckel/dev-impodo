"""Preserve correction values and complete worksheet output safely in Excel."""

from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook
from openpyxl.worksheet._writer import ALL_TEMP_FILES

from impodo.adapters.artifacts.attention_workbook import build_attention_workbook
from impodo.adapters.artifacts.reporting import ReportGenerationError
from impodo.domain.preflight.attention import (
    AttentionExport,
    AttentionFinding,
    AttentionRecord,
)


def example_export():
    return AttentionExport(
        workspace_id="workspace",
        project="Fictional correction",
        data_version_id="delivery",
        company="Company A",
        prepared_at=datetime(2026, 11, 12, tzinfo=UTC),
        comparison_at=None,
        quality_run_id="quality",
        comparison_id="",
        snapshots=(),
        records=(
            AttentionRecord(
                "row",
                "Sales/lines",
                "000123",
                18,
                {"table": (18, 42)},
                {"name": "=1+1", "code": "000123"},
                {"name": "Name", "code": "Reference"},
                {"name": ("column",)},
            ),
        ),
        findings=(
            AttentionFinding(
                "row",
                "Sales/lines",
                ("name",),
                "Must fix",
                "Name needs review",
                "Check this name.",
                "Confirm the intended name.",
                "Source data",
                "Data manager",
                "Needs correction before inclusion",
                "EXAMPLE",
            ),
        ),
        source_names={"table": "sales.xlsx · Lines"},
        source_columns={"column": "Name"},
        filtered_count=0,
    )


class AttentionWorkbookTests(TestCase):
    def test_large_duplicate_group_keeps_every_peer_without_quadratic_cell_lists(self):
        export = example_export()
        records = tuple(
            replace(
                export.records[0],
                row_id=f"row-{index}",
                record=f"R{index}",
                source_row=index + 2,
            )
            for index in range(1_001)
        )
        peers = tuple(record.row_id for record in records)
        findings = tuple(
            replace(
                export.findings[0],
                row_id=record.row_id,
                related_rows=peers,
                group_id="D0001",
            )
            for record in records
        )
        content = build_attention_workbook(
            replace(export, records=records, findings=findings), {}
        )
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["Duplicate groups"].max_row - 3, 1_001)
        self.assertEqual(workbook["Needs attention"].max_row - 3, 1_001)
        self.assertIn("Duplicate groups", workbook["Needs attention"]["L4"].value)
        self.assertLess(len(workbook["Needs attention"]["L4"].value), 100)
        self.assertTrue(workbook["Needs attention"]["L4"].hyperlink)

    def test_literal_values_complete_locations_links_and_highlight(self):
        content = build_attention_workbook(
            example_export(),
            {
                ("table", 18): {"column": '=HYPERLINK("https://invalid")'},
                ("table", 42): {"column": "Original name"},
            },
        )
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["Needs attention"]["D4"].value, "000123")
        self.assertIn("18, 42", workbook["Needs attention"]["F4"].value)
        sheet = next(
            sheet for sheet in workbook.worksheets if sheet.title.startswith("Data ·")
        )
        self.assertTrue(sheet["D4"].hyperlink)
        self.assertTrue(sheet.auto_filter.ref)
        self.assertEqual(sheet.freeze_panes, "D4")
        self.assertEqual(sheet["E4"].value, "000123")
        self.assertEqual(sheet["F4"].value, "=1+1")
        self.assertEqual(sheet["F4"].data_type, "s")
        self.assertEqual(sheet["F4"].fill.fgColor.rgb, "00FCE8E7")
        for row in sheet:
            self.assertFalse(any(cell.data_type == "f" for cell in row))

    def test_findings_split_without_truncation(self):
        export = example_export()
        export = replace(
            export,
            findings=tuple(
                replace(export.findings[0], code=str(index)) for index in range(5)
            ),
        )
        with patch("impodo.adapters.artifacts.attention_workbook._MAX_ROWS", 2):
            content = build_attention_workbook(export, {})
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        sheets = [
            sheet
            for sheet in workbook.worksheets
            if sheet.title.startswith("Needs attention")
        ]
        self.assertEqual(len(sheets), 3)
        self.assertEqual(sum(sheet.max_row - 3 for sheet in sheets), 5)

    def test_affected_data_and_duplicate_members_split_completely(self):
        export = example_export()
        records = tuple(
            replace(export.records[0], row_id=f"row-{index}", source_row=index + 2)
            for index in range(5)
        )
        peers = tuple(record.row_id for record in records)
        findings = tuple(
            replace(
                export.findings[0],
                row_id=record.row_id,
                related_rows=peers,
                group_id="D0001",
            )
            for record in records
        )
        with patch("impodo.adapters.artifacts.attention_workbook._MAX_ROWS", 2):
            content = build_attention_workbook(
                replace(export, records=records, findings=findings), {}
            )
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        for prefix in ("Needs attention", "Data ·", "Duplicate groups"):
            with self.subTest(prefix=prefix):
                sheets = [
                    sheet
                    for sheet in workbook.worksheets
                    if sheet.title.startswith(prefix)
                ]
                self.assertEqual(len(sheets), 3)
                self.assertEqual(sum(sheet.max_row - 3 for sheet in sheets), 5)

    def test_too_many_columns_fail_explicitly(self):
        with (
            patch("impodo.adapters.artifacts.attention_workbook._MAX_COLUMNS", 5),
            self.assertRaisesRegex(ReportGenerationError, "column limit"),
        ):
            build_attention_workbook(example_export(), {})

    def test_long_text_fails_explicitly_instead_of_truncating(self):
        temporary_files = set(ALL_TEMP_FILES)
        export = example_export()
        export = replace(
            export, findings=(replace(export.findings[0], explanation="a" * 32_768),)
        )
        with self.assertRaisesRegex(ReportGenerationError, "text limit"):
            build_attention_workbook(export, {})
        self.assertEqual(set(ALL_TEMP_FILES), temporary_files)
