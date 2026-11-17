"""Verify omission coverage without treating absent evidence as a clean result."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from openpyxl import load_workbook

from impodo.adapters.artifacts.profile_loader import load_profile
from impodo.adapters.artifacts.reporting import (
    write_preflight_outputs,
    write_review_workbook,
)
from impodo.domain.compiler import compile_profile_document
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
    field_set_aside_evidence,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceInput,
    OriginalSourceValue,
)
from impodo.domain.preflight.reports import (
    ReviewWorkbookCellEffect,
    ReviewWorkbookEvidence,
)
from impodo.domain.shared.models import Classification, PreparedRecord
from impodo.domain.source_snapshot import EncodedSourceCell
from tests.integration.artifacts.test_preflight_outputs import ROOT, golden_result
from tests.integration.artifacts.test_reporting_cli import _prepared_records


def _fields(
    findings: tuple[FieldSetAside, ...] = (),
    *,
    coverage_gaps: tuple[str, ...] = (),
) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        findings=findings,
        source_labels={},
        source_keys={},
        null_omissions={},
        upsert_datasets=frozenset(),
        target_models={"partners": "res.partner"},
        coverage_gaps=coverage_gaps,
    )


def _evidence(
    fields: FieldSetAsideEvidence | None,
    *,
    records: tuple[PreparedRecord, ...] = (),
    effects: tuple[ReviewWorkbookCellEffect, ...] = (),
    normalization_hash: str = "",
) -> ReviewWorkbookEvidence:
    return ReviewWorkbookEvidence(
        frozen_input_hash="",
        records=records,
        dataset_labels={},
        target_model_labels={},
        target_field_labels={},
        normalization_content_hash=normalization_hash,
        cell_effects=effects,
        target_field_required={},
        fields_set_aside=fields,
    )


def _empty_manifest(directory: Path, major: int = 20, mode: str = "local") -> Path:
    result = golden_result()
    result = replace(
        result,
        fingerprint=replace(
            result.fingerprint, odoo_version=f"{major}.0", connection_mode=mode
        ),
        decisions=(),
        reference_resolutions=(),
        issues=(),
        metadata_coverage=(),
    )
    manifest, _ = write_preflight_outputs(result, directory)
    return manifest


def _projection(path: Path) -> tuple[object, ...]:
    with closing(load_workbook(path)) as workbook:
        sheet = workbook["Fields set aside"]
        overview = workbook["Review overview"]["A25"]
        return (
            sheet["A2"].value,
            sheet["A4"].value,
            sheet["D4"].value,
            sheet["G4"].value,
            tuple(sheet.iter_rows(min_row=6, values_only=True)),
            sheet.freeze_panes,
            sheet.auto_filter.ref,
            overview.value,
            overview.hyperlink.location,
        )


class FieldsSetAsideCoverageTests(unittest.TestCase):
    def test_current_producer_keeps_zero_findings_partial_for_both_majors(self) -> None:
        plan = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        fields = field_set_aside_evidence(plan, (), {}, {})
        self.assertEqual(fields.findings, ())
        self.assertTrue(any("unmapped" in gap for gap in fields.coverage_gaps))
        self.assertTrue(any("Original" in gap for gap in fields.coverage_gaps))
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    directory = Path(temporary)
                    manifest = _empty_manifest(directory, major, mode)
                    path = write_review_workbook(
                        manifest,
                        directory / "review.xlsx",
                        review_evidence=_evidence(fields),
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        summary = sheet["A2"].value
                        self.assertTrue(summary.startswith("Partial coverage:"))
                        self.assertIn("unmapped", summary)
                        self.assertEqual(
                            sheet["A7"].value, "No saved field omissions shown"
                        )
                        self.assertIn(summary, workbook["Review overview"]["A25"].value)

    def test_only_explicit_complete_evidence_allows_definitive_zero(self) -> None:
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    directory = Path(temporary)
                    manifest = _empty_manifest(directory, major, mode)
                    path = write_review_workbook(
                        manifest,
                        directory / "review.xlsx",
                        review_evidence=_evidence(_fields()),
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        self.assertTrue(
                            sheet["A2"].value.startswith("Complete coverage")
                        )
                        self.assertEqual(sheet["A7"].value, "No fields were set aside")
                        self.assertEqual(sheet["G4"].value, "0 saved findings")
                        self.assertIn(
                            sheet["A2"].value, workbook["Review overview"]["A25"].value
                        )

    def test_manifest_without_preparation_evidence_reports_limited_coverage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _empty_manifest(directory)
            path = write_review_workbook(manifest, directory / "review.xlsx")
            with closing(load_workbook(path)) as workbook:
                sheet = workbook["Fields set aside"]
                self.assertTrue(sheet["A2"].value.startswith("Limited coverage:"))
                self.assertIn(
                    "preparation field evidence is unavailable", sheet["A2"].value
                )
                self.assertEqual(sheet["A7"].value, "No saved field omissions shown")
                self.assertIn(
                    sheet["A2"].value, workbook["Review overview"]["A25"].value
                )

    def test_coverage_counts_outcomes_without_confusing_blanks_and_columns(
        self,
    ) -> None:
        findings = (
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                2,
                ("code",),
                "ref",
                "Invalid code.",
                (OriginalSourceInput("code", OriginalSourceValue.from_cell(EncodedSourceCell.from_python("00123"))),),
            ),
            FieldSetAside(
                FieldSetAsideReason.RULE,
                "partners",
                3,
                ("phone",),
                "phone",
                "Preserve the existing phone.",
                (OriginalSourceInput("phone", OriginalSourceValue.from_cell(EncodedSourceCell.from_python(""))),),
            ),
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                4,
                ("date",),
                "date_start",
                "Invalid start date.",
                (OriginalSourceInput("date", OriginalSourceValue.unavailable()),),
            ),
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                4,
                ("date",),
                "date_end",
                "Invalid end date.",
                (OriginalSourceInput("date", OriginalSourceValue.unavailable()),),
            ),
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                5,
                ("first", "last"),
                "name",
                "Combined value rejected.",
                (),
            ),
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                6,
                (),
                "email",
                "Source coordinate unavailable.",
            ),
            FieldSetAside(
                FieldSetAsideReason.EXCLUDED,
                "partners",
                None,
                ("notes",),
                "comment",
                "Validation only.",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _empty_manifest(directory)
            path = write_review_workbook(
                manifest,
                directory / "review.xlsx",
                review_evidence=_evidence(_fields(findings)),
            )
            with closing(load_workbook(path)) as workbook:
                sheet = workbook["Fields set aside"]
                summary = sheet["A2"].value
                self.assertTrue(summary.startswith("Partial coverage:"))
                self.assertIn("4 finding(s) have no available original value", summary)
                self.assertIn(
                    "2 finding(s) have no verified source-cell coordinates", summary
                )
                self.assertEqual(sheet["A4"].value, "3 identified source cells")
                self.assertEqual(sheet["D4"].value, "1 whole column")
                self.assertEqual(sheet["G4"].value, "7 saved findings")
                rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
                self.assertEqual(sum(row[4] == "Unavailable" for row in rows), 4)
                self.assertIsNone(next(row[4] for row in rows if row[3] == "phone"))
                self.assertIsNone(next(row[4] for row in rows if row[3] == "notes"))
                self.assertEqual(
                    next(row[4] for row in rows if row[3] == "code"), "00123"
                )
                self.assertEqual(
                    {row[5] for row in rows if row[3] == "date"},
                    {"Date Start", "Date End"},
                )
                self.assertIn(summary, workbook["Review overview"]["A25"].value)

    def test_preparation_display_feedback_is_not_exported_as_original(
        self,
    ) -> None:
        result = golden_result()
        decision = next(
            item
            for item in result.decisions
            if item.classification is Classification.UNCHANGED
        )
        result = replace(
            result, decisions=(decision,), reference_resolutions=(), issues=()
        )
        records = _prepared_records(result)
        record = records[0]
        fields = _fields(
            (
                FieldSetAside(
                    FieldSetAsideReason.INVALID,
                    record.dataset,
                    record.source_row,
                    ("name",),
                    "name",
                    "A saved omission with no exact source representation.",
                ),
            )
        )
        normalization_hash = "sha256:" + "3" * 64
        for before in ("00123", "—", "Left / Right", "Hidden for restricted data"):
            with (
                self.subTest(before=before),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                manifest, path = write_preflight_outputs(result, directory)
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["preflight_evidence"] = {
                    "normalization_content_hash": normalization_hash
                }
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                effect = ReviewWorkbookCellEffect(
                    record.source_trace_id,
                    record.dataset,
                    record.source_row,
                    "name",
                    before,
                    str(record.scalar_values["name"]),
                    "Saved preparation",
                    "Recorded representation.",
                )
                write_review_workbook(
                    manifest,
                    path,
                    review_evidence=_evidence(
                        fields,
                        records=records,
                        effects=(effect,),
                        normalization_hash=normalization_hash,
                    ),
                )
                with closing(load_workbook(path)) as workbook:
                    sheet = workbook["Fields set aside"]
                    self.assertTrue(sheet["A2"].value.startswith("Partial coverage:"))
                    self.assertIn("no available original value", sheet["A2"].value)
                    self.assertEqual(sheet["E7"].value, "Unavailable")
                    self.assertIn(
                        sheet["A2"].value, workbook["Review overview"]["A25"].value
                    )

    def test_normalization_to_null_keeps_original_representation_unconfirmed(
        self,
    ) -> None:
        result = golden_result()
        decision = next(
            item
            for item in result.decisions
            if item.classification is Classification.UNCHANGED
        )
        result = replace(
            result, decisions=(decision,), reference_resolutions=(), issues=()
        )
        record = replace(_prepared_records(result)[0], scalar_values={"name": None})
        fields = replace(_fields(), source_keys={(record.dataset, "name"): ("name",)})
        normalization_hash = "sha256:" + "4" * 64
        effect = ReviewWorkbookCellEffect(
            record.source_trace_id,
            record.dataset,
            record.source_row,
            "name",
            "00123",
            "—",
            "Saved preparation",
            "The rule omitted the value.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest, path = write_preflight_outputs(result, directory)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["preflight_evidence"] = {
                "normalization_content_hash": normalization_hash
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            write_review_workbook(
                manifest,
                path,
                review_evidence=_evidence(
                    fields,
                    records=(record,),
                    effects=(effect,),
                    normalization_hash=normalization_hash,
                ),
            )
            with closing(load_workbook(path)) as workbook:
                sheet = workbook["Fields set aside"]
                self.assertEqual(sheet["A7"].value, "Skipped by rule")
                self.assertEqual(sheet["E7"].value, "Unavailable")
                self.assertEqual(sheet["G4"].value, "1 saved findings")
                self.assertTrue(sheet["A2"].value.startswith("Partial coverage:"))
                self.assertIn("no available original value", sheet["A2"].value)
                self.assertIn(
                    sheet["A2"].value, workbook["Review overview"]["A25"].value
                )

    def test_protected_source_withholds_populated_field_findings(self) -> None:
        secret = "private-original-never-export"
        fields = _fields(
            (
                FieldSetAside(
                    FieldSetAsideReason.INVALID,
                    "partners",
                    2,
                    (secret,),
                    "name",
                    secret,
                    (OriginalSourceInput(secret, OriginalSourceValue.from_cell(EncodedSourceCell.from_python(secret))),),
                ),
            ),
            coverage_gaps=(secret,),
        )
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    directory = Path(temporary)
                    manifest = directory / "manifest.json"
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
                    previews = directory / "previews"
                    path = write_review_workbook(
                        manifest,
                        directory / "review.xlsx",
                        preview_directory=previews,
                        review_evidence=_evidence(fields),
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        self.assertIn("withheld", sheet["A2"].value)
                        self.assertEqual(
                            sheet["A7"].value, "No saved field omissions shown"
                        )
                        self.assertEqual(sheet["G4"].value, "0 saved findings")
                        self.assertNotIn(secret, str(tuple(sheet.values)))
                        self.assertNotIn(
                            secret, workbook["Review overview"]["A25"].value
                        )
                    self.assertNotIn(
                        secret,
                        (previews / "fields-set-aside.csv").read_text(
                            encoding="utf-8-sig"
                        ),
                    )

    def test_recreation_reuses_saved_findings_counts_and_coverage(self) -> None:
        fields = _fields(
            (
                FieldSetAside(
                    FieldSetAsideReason.INVALID,
                    "partners",
                    2,
                    ("code",),
                    "ref",
                    "Saved invalid value.",
                    (OriginalSourceInput("code", OriginalSourceValue.from_cell(EncodedSourceCell.from_python("=1+2"))),),
                ),
            ),
            coverage_gaps=("Unrecorded source exclusions.",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _empty_manifest(directory)
            saved_manifest = manifest.read_bytes()
            evidence = _evidence(fields)
            first = write_review_workbook(
                manifest, directory / "first.xlsx", review_evidence=evidence
            )
            recreated = write_review_workbook(
                manifest, directory / "recreated.xlsx", review_evidence=evidence
            )
            self.assertEqual(_projection(first), _projection(recreated))
            self.assertEqual(manifest.read_bytes(), saved_manifest)
            self.assertEqual(evidence.fields_set_aside, fields)
