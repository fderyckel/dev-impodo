from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import (
    MANIFEST_NAME,
    WORKBOOK_NAME,
    write_preflight_outputs,
    write_review_workbook,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.frozen_input import ReviewWorkbookOmittedRow
from impodo.domain.preflight.reports import (
    ReviewWorkbookCellEffect,
    ReviewWorkbookEvidence,
)
from impodo.domain.preflight.deferred_scope import (
    DeferredIssue,
    DeferredIssueGroup,
    DeferredIssueScope,
    DeferredOmittedRow,
    DeferredScopePreview,
)
from impodo.domain.shared.models import Issue, PreparedRecord, Severity
from impodo.web.composition.cli import build_parser, main
from tests.integration.artifacts.test_preflight_outputs import ROOT, golden_result


class CliTests(unittest.TestCase):
    def test_help_lists_read_only_commands(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("profile", help_text)
        self.assertIn("snapshot-metadata", help_text)
        self.assertIn("snapshot-records", help_text)
        self.assertIn("preflight", help_text)
        self.assertNotIn("import", help_text.casefold())

    def test_profile_command_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "prepared.json"
            exit_code = main(
                [
                    "profile",
                    "--profile",
                    str(ROOT / "profiles/examples/bom.yaml"),
                    "--input",
                    str(ROOT / "examples/bom"),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["profile"], {"id": "bom_example"})
            self.assertEqual(len(payload["records"]), 3)
            self.assertNotIn("odoo_id", output.read_text())


class WorkbookIntegrationTests(unittest.TestCase):
    def test_reviewed_set_aside_keeps_full_evidence_and_reduces_active_sheets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = golden_result()
            prepared_records = _prepared_records(result)
            manifest_path, workbook_path = write_preflight_outputs(
                result,
                Path(directory) / "report",
                prepared_records=prepared_records,
            )
            blocked = next(
                decision
                for decision in result.decisions
                if decision.business_identity == ("P-BLOCK",)
            )
            issue_id = "sha256:" + "1" * 64
            row_id = "sha256:" + "2" * 64
            preview = DeferredScopePreview(
                comparison_id="comparison-1",
                comparison_hash=result.semantic_hash,
                execution_snapshot_hash="sha256:" + "3" * 64,
                selected_issue_ids=(issue_id,),
                groups=(
                    DeferredIssueGroup(
                        group_id="sha256:" + "4" * 64,
                        issue_id=issue_id,
                        root_row_id=row_id,
                        row_ids=(row_id,),
                        counts_by_dataset=((blocked.dataset, 1),),
                        write_count=0,
                    ),
                ),
                omitted_rows=(
                    DeferredOmittedRow(
                        row_id=row_id,
                        dataset=blocked.dataset,
                        source_row=blocked.source_row,
                        source_trace_id=blocked.source_trace_id,
                        disposition=blocked.classification.value,
                        direct_issue_ids=(issue_id,),
                        inherited_issue_ids=(),
                    ),
                ),
                counts_by_dataset=((blocked.dataset, 1),),
                prepared_record_count=len(result.decisions) + 2,
                already_set_aside_count=2,
                original_write_count=2,
                omitted_write_count=0,
                remaining_write_count=2,
                remaining_problem_record_count=0,
                remaining_run_issue_count=0,
            )
            evidence = ReviewWorkbookEvidence(
                frozen_input_hash="",
                records=prepared_records,
                dataset_labels={
                    item.dataset: item.dataset.replace("_", " ").title()
                    for item in prepared_records
                },
                target_model_labels={},
                target_field_labels={},
                normalization_content_hash="",
                cell_effects=(),
                target_field_required={},
                omitted_rows=(
                    ReviewWorkbookOmittedRow("bom_lines", 20, "FILTERED", True, "Use active BoMs."),
                    ReviewWorkbookOmittedRow("bom_lines", 21, "INVALID", False, "Invalid sequence."),
                ),
            )

            write_review_workbook(
                manifest_path,
                workbook_path,
                review_evidence=evidence,
                deferred_preview=preview,
                deferred_issues=(
                    DeferredIssue(
                        issue_id=issue_id,
                        code="REFERENCE_NOT_FOUND",
                        scope=DeferredIssueScope.ROW,
                        row_id=row_id,
                        field="uom_id",
                        message="The related unit is missing in Odoo.",
                    ),
                ),
            )

            workbook = load_workbook(workbook_path, data_only=True)
            self.assertIn("Deferred issues", workbook.sheetnames)
            overview = workbook["Review overview"]
            self.assertEqual(overview["E5"].value, "Ready with records set aside")
            self.assertEqual(overview["E7"].value, 2)
            self.assertEqual(overview["E8"].value, 1)
            self.assertEqual(overview["E9"].value, 2)
            active_values = {
                str(cell.value)
                for row in workbook["Records to load"].iter_rows(min_row=4)
                for cell in row
                if cell.value is not None
            }
            self.assertNotIn("P-BLOCK", active_values)
            self.assertNotIn("FILTERED", active_values)
            self.assertNotIn("INVALID", active_values)
            filters = workbook["Excluded by filters"]
            self.assertEqual(
                next(filters.iter_rows(min_row=4, values_only=True)),
                ("bom_lines", 20, "FILTERED", "Use active BoMs."),
            )
            self.assertIn("Excluded by filters · 1 row", overview["A26"].value)
            self.assertIn("Deferred issues · 2 rows", overview["A27"].value)
            deferred = workbook["Deferred issues"]
            headers = [cell.value for cell in deferred[3]]
            prep_row, deferred_row = deferred.iter_rows(min_row=4, values_only=True)
            self.assertEqual(prep_row[headers.index("Record")], "INVALID")
            self.assertEqual(prep_row[headers.index("Stage")], "Prepare data")
            self.assertEqual(prep_row[headers.index("Details")], "Invalid sequence.")
            self.assertEqual(deferred_row[headers.index("Stage")], "Final review")
            self.assertEqual(deferred_row[headers.index("Source row")], blocked.source_row)
            self.assertEqual(deferred_row[headers.index("Record")], "P-BLOCK")
            self.assertEqual(deferred_row[headers.index("Reason type")], "Direct")
            self.assertEqual(
                deferred_row[headers.index("Root issue")],
                "REFERENCE_NOT_FOUND",
            )
            self.assertEqual(
                deferred_row[headers.index("Comparison")],
                preview.comparison_id,
            )
            workbook.close()

    def test_review_workbook_and_manifest_are_generated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            result = golden_result()
            blocked_trace = next(
                decision.source_trace_id
                for decision in result.decisions
                if decision.business_identity == ("P-BLOCK",)
            )
            prepared_records = tuple(
                (
                    replace(
                        record,
                        scalar_values={**record.scalar_values, "uom_id": "MISSING"},
                    )
                    if record.source_trace_id == blocked_trace
                    else record
                )
                for record in _prepared_records(result)
            )
            manifest_path, workbook_path = write_preflight_outputs(
                result,
                output,
                prepared_records=prepared_records,
            )
            self.assertEqual(manifest_path.name, MANIFEST_NAME)
            self.assertEqual(workbook_path.name, WORKBOOK_NAME)
            manifest_text = manifest_path.read_text()
            self.assertNotIn("odoo_id", manifest_text)
            with zipfile.ZipFile(workbook_path) as archive:
                archive_names = archive.namelist()
                workbook_xml = archive.read("xl/workbook.xml").decode()
                worksheet_xml = [
                    archive.read(name).decode()
                    for name in archive_names
                    if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                ]
            self.assertFalse(
                any(name.startswith("xl/tables/") for name in archive_names)
            )
            self.assertFalse(any("<tableParts" in xml for xml in worksheet_xml))
            self.assertTrue(all(xml.count("<autoFilter") <= 1 for xml in worksheet_xml))
            for sheet_name in (
                "Review overview",
                "Needs attention",
                "Records to load",
                "Changes to Odoo",
                "Evidence",
            ):
                self.assertIn(sheet_name, workbook_xml)
            workbook = load_workbook(workbook_path, data_only=False)
            self.assertEqual(
                workbook["Changes to Odoo"]["E3"].value,
                "Value that will be loaded",
            )
            self.assertEqual(
                workbook["Changes to Odoo"]["F3"].value,
                "Current Odoo value",
            )
            self.assertIsInstance(workbook["Review overview"]["B5"].value, int)
            self.assertEqual(
                workbook["Review overview"]["A11"].value,
                "Prepared value feedback",
            )
            self.assertEqual(
                workbook["Review overview"]["D11"].value,
                "How to use this workbook",
            )
            self.assertTrue(
                workbook["Review overview"]["A1"].fill.fgColor.rgb.endswith("292C28")
            )
            records = workbook["Records to load"]
            headers = [cell.value for cell in records[3]]
            name_column = headers.index("Name") + 1
            self.assertEqual(records.cell(4, name_column).value, "Prepared PARTNER-NEW")
            self.assertEqual(records["A4"].value, "Ready")
            attention = workbook["Needs attention"]
            attention_headers = [cell.value for cell in attention[3]]
            self.assertEqual(attention_headers[0], "Action priority")
            self.assertIn("Final prepared value", attention_headers)
            self.assertEqual(
                workbook["Review overview"]["E6"].value,
                sum(
                    row[0] == "Must fix"
                    for row in attention.iter_rows(min_row=4, values_only=True)
                ),
            )
            technical_code_column = attention_headers.index("Technical code")
            prepared_value_column = attention_headers.index("Final prepared value")
            reference_row = next(
                row
                for row in attention.iter_rows(min_row=4, values_only=True)
                if row[technical_code_column] == "REFERENCE_NOT_FOUND"
                and row[2] == "P-BLOCK"
            )
            self.assertEqual(reference_row[0], "Must fix")
            self.assertEqual(reference_row[prepared_value_column], "MISSING")
            attention_text = " ".join(
                str(cell.value or "")
                for row in attention.iter_rows(min_row=4)
                for cell in row
            )
            self.assertIn("A related record cannot be found.", attention_text)
            self.assertIn("REFERENCE_NOT_FOUND", attention_text)
            self.assertFalse(
                any(
                    cell.data_type == "f"
                    for sheet in workbook.worksheets
                    for row in sheet.iter_rows()
                    for cell in row
                )
            )
            for sheet in workbook.worksheets[1:]:
                self.assertEqual(len(sheet.tables), 0)
                header_row = 6 if sheet.title == "Fields set aside" else 3
                if sheet.max_row > header_row:
                    last_column = sheet.cell(
                        header_row,
                        sheet.max_column,
                    ).column_letter
                    self.assertEqual(
                        sheet.auto_filter.ref,
                        f"A{header_row}:{last_column}{sheet.max_row}",
                    )
                else:
                    self.assertIsNone(sheet.auto_filter.ref)
            workbook.close()

    def test_prepared_cells_show_final_values_with_frozen_change_feedback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            result = golden_result()
            prepared_records = _prepared_records(result)
            manifest_path, workbook_path = write_preflight_outputs(result, output)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            frozen_input_hash = "sha256:" + "a" * 64
            normalization_content_hash = "sha256:" + "b" * 64
            manifest["preflight_evidence"] = {
                "frozen_input_hash": frozen_input_hash,
                "normalization_content_hash": normalization_content_hash,
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            changed_record = prepared_records[0]
            evidence = ReviewWorkbookEvidence(
                frozen_input_hash=frozen_input_hash,
                records=prepared_records,
                dataset_labels={
                    item.dataset: item.dataset.replace("_", " ").title()
                    for item in prepared_records
                },
                target_model_labels={"res.partner": "Contact"},
                target_field_labels={
                    ("res.partner", "name"): "Name",
                    ("res.partner", "email"): "Email",
                },
                normalization_content_hash=normalization_content_hash,
                cell_effects=(
                    ReviewWorkbookCellEffect(
                        source_trace_id=changed_record.source_trace_id,
                        dataset=changed_record.dataset,
                        source_row=changed_record.source_row,
                        target_field="name",
                        before=" Raw contact name ",
                        after=str(changed_record.scalar_values["name"]),
                        rule_name="Trim surrounding spaces",
                        explanation="Impodo removed surrounding spaces.",
                    ),
                    ReviewWorkbookCellEffect(
                        source_trace_id=changed_record.source_trace_id,
                        dataset=changed_record.dataset,
                        source_row=changed_record.source_row,
                        target_field="email",
                        before="—",
                        after=str(changed_record.scalar_values["email"]),
                        rule_name="Supply fallback email",
                        explanation="Impodo supplied the configured fallback.",
                    ),
                    ReviewWorkbookCellEffect(
                        source_trace_id=changed_record.source_trace_id,
                        dataset=changed_record.dataset,
                        source_row=changed_record.source_row,
                        target_field="ref",
                        before=" partner-new ",
                        after=str(changed_record.target_identity[0]),
                        rule_name="Prepare the business key",
                        explanation="Impodo normalized the contact key.",
                    ),
                ),
                target_field_required={
                    ("res.partner", "name"): True,
                    ("res.partner", "email"): False,
                },
            )

            write_review_workbook(
                manifest_path,
                workbook_path,
                review_evidence=evidence,
            )

            workbook = load_workbook(workbook_path, data_only=False)
            records = workbook["Records to load"]
            headers = [cell.value for cell in records[3]]
            name_column = headers.index("Name") + 1
            email_column = headers.index("Email") + 1
            feedback_column = headers.index("Prepared value feedback") + 1
            name_cell = records.cell(4, name_column)
            email_cell = records.cell(4, email_column)
            identity_cell = records.cell(4, 5)
            self.assertEqual(
                name_cell.value,
                changed_record.scalar_values["name"],
            )
            self.assertNotIn("Raw contact name", str(name_cell.value))
            self.assertIn("Changed by Impodo", name_cell.comment.text)
            self.assertIn("Original value:  Raw contact name ", name_cell.comment.text)
            self.assertTrue(name_cell.fill.fgColor.rgb.endswith("EAF2FB"))
            self.assertIn("Added by Impodo", email_cell.comment.text)
            self.assertTrue(email_cell.fill.fgColor.rgb.endswith("EDF7EF"))
            self.assertIn("Changed by Impodo", identity_cell.comment.text)
            self.assertNotIn("Ref", headers)
            self.assertIn(
                "Name: Changed by Impodo",
                str(records.cell(4, feedback_column).value),
            )
            self.assertIn(
                "Business identity: Changed by Impodo",
                str(records.cell(4, feedback_column).value),
            )
            self.assertEqual(workbook["Review overview"]["B12"].value, 2)
            self.assertEqual(workbook["Review overview"]["B13"].value, 1)
            workbook.close()

    def test_fields_set_aside_preserves_saved_reasons_and_source_text(self) -> None:
        for major in (19, 20):
            for connection_mode in ("local", "remote"):
                with (
                    self.subTest(major=major, connection_mode=connection_mode),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    result = golden_result()
                    result = replace(result, fingerprint=replace(
                        result.fingerprint, odoo_version=f"{major}.0",
                        connection_mode=connection_mode,
                    ))
                    records = _prepared_records(result)
                    record = records[0]
                    record = replace(record, scalar_values={**record.scalar_values, "phone": None})
                    records = (record, *records[1:])
                    manifest_path, workbook_path = write_preflight_outputs(result, directory)
                    fields = FieldSetAsideEvidence(
                        findings=(
                            FieldSetAside(
                                FieldSetAsideReason.INVALID, record.dataset, 18,
                                ("date",), "date", "The date could not be read.",
                                "=1+2",
                            ),
                            FieldSetAside(
                                FieldSetAsideReason.INVALID, record.dataset, 18,
                                ("date",), "date_end", "The date could not be read.",
                                "",
                            ),
                            FieldSetAside(
                                FieldSetAsideReason.EXCLUDED, record.dataset, None,
                                ("notes",), "comment", "Validation only.",
                            ),
                        ),
                        source_labels={
                            (record.dataset, "date"): "Start date",
                            (record.dataset, "notes"): "Internal notes",
                            (record.dataset, "phone"): "Phone",
                        },
                        source_keys={(record.dataset, "phone"): ("phone",)},
                        null_omissions={record.dataset: ("phone",)},
                        upsert_datasets=frozenset(),
                        target_models={record.dataset: "res.partner"},
                    )
                    evidence = ReviewWorkbookEvidence(
                        frozen_input_hash="", records=records, dataset_labels={},
                        target_model_labels={}, target_field_labels={},
                        normalization_content_hash="", cell_effects=(),
                        target_field_required={}, fields_set_aside=fields,
                    )
                    previews = Path(directory) / "previews"
                    write_review_workbook(
                        manifest_path, workbook_path, review_evidence=evidence,
                        preview_directory=previews,
                    )

                    workbook = load_workbook(workbook_path)
                    self.addCleanup(workbook.close)
                    sheet = workbook["Fields set aside"]
                    self.assertEqual(workbook.sheetnames[1:3], ["Needs attention", sheet.title])
                    self.assertEqual(sheet.freeze_panes, "A7")
                    self.assertTrue(sheet.auto_filter.ref.startswith("A6:I"))
                    self.assertEqual(sheet["A4"].value, "2 identified source cells")
                    self.assertEqual(sheet["D4"].value, "1 whole column")
                    self.assertEqual(sheet["D7"].value, "Start date")
                    self.assertEqual(sheet["E7"].value, "=1+2")
                    self.assertEqual(sheet["E7"].data_type, "s")
                    self.assertIsNone(sheet["E8"].value)
                    self.assertTrue(sheet["A7"].fill.fgColor.rgb.endswith("FCE8E7"))
                    rule_cell = next(cell for cell in sheet["A"] if cell.value == "Skipped by rule")
                    self.assertTrue(rule_cell.fill.fgColor.rgb.endswith("EAF2FB"))
                    self.assertEqual(sheet.cell(rule_cell.row, 5).value, "Unavailable")
                    self.assertIn("'=1+2", (previews / "fields-set-aside.csv").read_text())
                    self.assertEqual(workbook["Review overview"]["A25"].hyperlink.location,
                                     "'Fields set aside'!A1")

    def test_fields_set_aside_explains_redacted_odoo_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for major in (19, 20):
                with self.subTest(major=major):
                    manifest = Path(directory) / "manifest.json"
                    manifest.write_text(json.dumps({
                        "contract": "odoo-pinned-comparison-v1",
                        "target": {"odoo_version": f"{major}.0"},
                    }), encoding="utf-8")
                    path = write_review_workbook(manifest, Path(directory) / "review.xlsx")
                    workbook = load_workbook(path)
                    self.addCleanup(workbook.close)
                    sheet = workbook["Fields set aside"]
                    self.assertIn("withheld", sheet["A2"].value)
                    self.assertEqual(sheet["A7"].value, "No saved field omissions shown")

    def test_workbook_csv_previews_need_no_external_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            previews = Path(directory) / "previews"
            write_preflight_outputs(
                golden_result(),
                output,
                preview_directory=previews,
            )
            self.assertTrue((previews / "review-overview.csv").is_file())
            self.assertTrue((previews / "changes-to-odoo.csv").is_file())

    def test_field_level_warning_is_not_repeated_as_a_record_defect(self) -> None:
        warning = Issue(
            code="TARGET_SELECTION_CHOICES_CHANGED",
            message=(
                "Current Odoo choices for product.template.tracking changed since mapping "
                "(0 removed, 1 added)"
            ),
            severity=Severity.WARNING,
            dataset="products",
            field="tracking",
        )
        original = golden_result()
        result = replace(
            original,
            decisions=tuple(
                (
                    replace(decision, issues=(*decision.issues, warning))
                    if decision.dataset == "products"
                    else decision
                )
                for decision in original.decisions
            ),
            issues=(*original.issues, warning),
        )
        with tempfile.TemporaryDirectory() as directory:
            _manifest_path, workbook_path = write_preflight_outputs(
                result,
                Path(directory) / "report",
                prepared_records=_prepared_records(result),
            )

            workbook = load_workbook(workbook_path, data_only=True)
            attention = workbook["Needs attention"]
            headers = [cell.value for cell in attention[3]]
            technical_code_column = headers.index("Technical code")
            priorities = [
                row[0] for row in attention.iter_rows(min_row=4, values_only=True)
            ]
            technical_codes = [
                row[technical_code_column]
                for row in attention.iter_rows(min_row=4, values_only=True)
            ]
            self.assertEqual(
                technical_codes.count("TARGET_SELECTION_CHOICES_CHANGED"),
                1,
            )
            self.assertEqual(priorities[-1], "Review")
            self.assertNotIn("Review", priorities[:-1])
            self.assertEqual(workbook["Review overview"]["E7"].value, 1)
            records = workbook["Records to load"]
            product_create = next(
                row
                for row in records.iter_rows(min_row=4, values_only=True)
                if row[4] == "P-CREATE"
            )
            self.assertEqual(product_create[0], "Ready")
            self.assertEqual(product_create[-1], "Ready to create. No action needed.")
            workbook.close()


def _prepared_records(result):
    return tuple(
        PreparedRecord(
            dataset=decision.dataset,
            source_row=decision.source_row,
            target_model="res.partner",
            source_identity=decision.business_identity,
            target_identity=decision.business_identity,
            target_scope=decision.business_scope,
            scalar_values={
                "name": f"Prepared {decision.business_identity[0]}",
                "email": "reviewer@example.invalid",
            },
            references={},
            source_trace_id=decision.source_trace_id,
            issues=(),
        )
        for decision in result.decisions
    )


if __name__ == "__main__":
    unittest.main()
