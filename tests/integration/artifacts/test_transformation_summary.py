"""Verify the first-tab transformation summary against frozen report evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import (
    ReportGenerationError,
    write_preflight_outputs,
    write_review_workbook,
)
from impodo.domain.preflight.reports import (
    ReviewWorkbookCellEffect,
    ReviewWorkbookEvidence,
)
from tests.integration.artifacts.test_preflight_outputs import golden_result
from tests.integration.artifacts.test_reporting_cli import _prepared_records


class TransformationSummaryTests(unittest.TestCase):
    def _fixture(self, directory: str, *, major: int = 19, mode: str = "local"):
        result = golden_result()
        result = replace(
            result,
            fingerprint=replace(
                result.fingerprint,
                odoo_version=f"{major}.0",
                connection_mode=mode,
            ),
        )
        manifest_path, workbook_path = write_preflight_outputs(result, directory)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["preflight_evidence"] = {
            "frozen_input_hash": "sha256:" + "a" * 64,
            "normalization_content_hash": "sha256:" + "b" * 64,
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        records = _prepared_records(result)
        evidence = ReviewWorkbookEvidence(
            frozen_input_hash=manifest["preflight_evidence"]["frozen_input_hash"],
            records=records,
            dataset_labels={item.dataset: item.dataset.title() for item in records},
            target_model_labels={},
            target_field_labels={
                ("res.partner", "name"): "Contact name",
                ("res.partner", "email"): "Email",
                ("res.partner", "ref"): "Reference",
            },
            normalization_content_hash=manifest["preflight_evidence"][
                "normalization_content_hash"
            ],
            cell_effects=(),
            target_field_required={},
        )
        return manifest_path, workbook_path, evidence

    def _effect(self, record, *, field="name", rule="Trim surrounding spaces"):
        return ReviewWorkbookCellEffect(
            source_trace_id=record.source_trace_id,
            dataset=record.dataset,
            source_row=record.source_row,
            target_field=field,
            before=" Raw source value ",
            after="Prepared value",
            rule_name=rule,
            explanation="Impodo removed surrounding spaces.",
        )

    def _summary_rows(self, workbook):
        sheet = workbook.worksheets[0]
        title = next(
            cell
            for cell in sheet["A"]
            if cell.value == "Stage 3 transformation summary"
        )
        rows = list(sheet.iter_rows(min_row=title.row + 4, values_only=True))
        return sheet, title.row, rows

    def test_groups_each_rule_and_field_without_counting_a_record_twice(self) -> None:
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    manifest_path, path, evidence = self._fixture(
                        directory, major=major, mode=mode
                    )
                    products = [
                        record
                        for record in evidence.records
                        if record.dataset == "products"
                    ]
                    partner = next(
                        record
                        for record in evidence.records
                        if record.dataset == "partners"
                    )
                    first, second = products[:2]
                    effect = self._effect(first)
                    effects = (
                        effect,
                        effect,
                        self._effect(second),
                        self._effect(partner),
                        self._effect(first, rule="Standardize letter case"),
                        self._effect(
                            first, field="ref", rule="Prepare the business key"
                        ),
                        replace(
                            self._effect(
                                first, field="email", rule="Supply fallback email"
                            ),
                            before="—",
                        ),
                    )
                    manifest_before = manifest_path.read_bytes()
                    write_review_workbook(
                        manifest_path,
                        path,
                        review_evidence=replace(evidence, cell_effects=effects),
                    )
                    workbook = load_workbook(path)
                    self.addCleanup(workbook.close)
                    sheet, title_row, rows = self._summary_rows(workbook)
                    self.assertEqual(sheet.title, "Review overview")
                    self.assertEqual(len(rows), 5)
                    counts = {(row[0], row[1], row[2]): row[7] for row in rows}
                    self.assertEqual(
                        counts[("Products", "Contact name", "Trim surrounding spaces")],
                        2,
                    )
                    self.assertEqual(
                        counts[("Partners", "Contact name", "Trim surrounding spaces")],
                        1,
                    )
                    self.assertEqual(
                        counts[("Products", "Contact name", "Standardize letter case")],
                        1,
                    )
                    self.assertEqual(
                        counts[("Products", "Reference", "Prepare the business key")], 1
                    )
                    self.assertEqual(
                        counts[("Products", "Email", "Supply fallback email")], 1
                    )
                    self.assertIn(
                        "counts can overlap", sheet.cell(title_row + 1, 1).value
                    )
                    self.assertIn(
                        "Only rules with recorded effects",
                        sheet.cell(title_row + 1, 1).value,
                    )
                    self.assertNotIn("Raw source value", str(rows))
                    self.assertTrue(all(isinstance(row[7], int) for row in rows))
                    self.assertIn(f"$A$1:$H${sheet.max_row}", str(sheet.print_area))
                    self.assertEqual(manifest_path.read_bytes(), manifest_before)

                    write_review_workbook(
                        manifest_path,
                        Path(directory) / "recreated.xlsx",
                        review_evidence=replace(
                            evidence, cell_effects=tuple(reversed(effects))
                        ),
                    )
                    recreated = load_workbook(Path(directory) / "recreated.xlsx")
                    self.addCleanup(recreated.close)
                    self.assertEqual(self._summary_rows(recreated)[2], rows)

    def test_distinguishes_no_recorded_changes_from_unavailable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, path, evidence = self._fixture(directory)
            for supplied, expected in (
                (evidence, "No value changes were recorded"),
                (None, "Saved transformation details are unavailable"),
                (
                    replace(evidence, normalization_content_hash=""),
                    "Saved transformation details are unavailable",
                ),
            ):
                with self.subTest(expected=expected, supplied=supplied is not None):
                    write_review_workbook(manifest_path, path, review_evidence=supplied)
                    workbook = load_workbook(path)
                    self.addCleanup(workbook.close)
                    sheet, title_row, rows = self._summary_rows(workbook)
                    self.assertIn(expected, sheet.cell(title_row + 1, 1).value)
                    self.assertEqual(rows, [])

    def test_odoo_source_summary_keeps_protected_details_out(self) -> None:
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    manifest = Path(directory) / "manifest.json"
                    manifest.write_text(
                        json.dumps(
                            {
                                "contract": "odoo-pinned-comparison-v1",
                                "target": {
                                    "odoo_version": f"{major}.0",
                                    "connection_mode": mode,
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    path = write_review_workbook(
                        manifest, Path(directory) / "review.xlsx"
                    )
                    workbook = load_workbook(path)
                    self.addCleanup(workbook.close)
                    sheet, title_row, rows = self._summary_rows(workbook)
                    self.assertIn("unavailable", sheet.cell(title_row + 1, 1).value)
                    self.assertEqual(rows, [])

    def test_summary_text_cannot_become_an_excel_formula(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, path, evidence = self._fixture(directory)
            record = evidence.records[0]
            effect = replace(
                self._effect(record, rule="=1+1"), explanation="@SUM(A1:A2)"
            )
            evidence = replace(
                evidence,
                cell_effects=(effect,),
                dataset_labels={record.dataset: "+Dataset"},
                target_field_labels={(record.target_model, "name"): "-Field"},
            )
            write_review_workbook(manifest_path, path, review_evidence=evidence)
            workbook = load_workbook(path)
            self.addCleanup(workbook.close)
            sheet, title_row, _rows = self._summary_rows(workbook)
            for column in (1, 2, 3, 5):
                cell = sheet.cell(title_row + 4, column)
                self.assertEqual(cell.data_type, "s")
                self.assertTrue(cell.value.startswith("'"))

    def test_mismatched_saved_evidence_still_blocks_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, path, evidence = self._fixture(directory)
            evidence = replace(
                evidence,
                normalization_content_hash="sha256:" + "c" * 64,
                cell_effects=(self._effect(evidence.records[0]),),
            )
            with self.assertRaisesRegex(
                ReportGenerationError, "feedback does not match"
            ):
                write_review_workbook(manifest_path, path, review_evidence=evidence)


if __name__ == "__main__":
    unittest.main()
