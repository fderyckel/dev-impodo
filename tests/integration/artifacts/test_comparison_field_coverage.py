"""Bind recovered comparison originals to exact saved bytes without duplicate rows."""

import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import (
    ReportGenerationError,
    write_review_workbook,
)
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.preflight.comparison_fields import (
    COMPARISON_RELATIONSHIP_CODES,
    ComparisonFieldCoverage,
    comparison_relationship_findings,
)
from impodo.domain.shared.models import Classification, Decision, Issue
from tests.integration.artifacts import test_relationship_original_values as fixtures


class ComparisonFieldCoverageExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def saved(self, code="REFERENCE_NOT_FOUND"):
        fixture = fixtures._fixture(
            self.root / code / "source", ("=1+2",), kind="many2one"
        )
        record = fixture.records[0]
        decision = Decision(
            record.dataset,
            record.source_row,
            record.target_identity,
            record.target_scope,
            Classification.BLOCKED,
            0,
            source_trace_id=record.source_trace_id,
            issues=(
                Issue(
                    code,
                    "Review the missing related record.",
                    dataset=record.dataset,
                    row=record.source_row,
                    field="category_id",
                ),
            ),
        )
        evidence = replace(fixture.plan(), records=(record,))
        initial = fixtures._workbook(
            self.root / code / "initial", evidence, decisions=(decision,)
        )
        manifest = initial.parent / "readiness_manifest.json"
        payload = json.loads(manifest.read_bytes())
        findings = comparison_relationship_findings(
            payload["decisions"],
            fixture.fields,
            records={record.source_trace_id: record},
        )
        fields, requests = plan_workbook_original_values(
            fields=replace(fixture.fields, findings=findings),
            selection=fixture.selection,
            physical_selection_hash=fixture.selection.content_hash,
            mappings=(fixture.mapping,),
            sources=fixture.sources,
            workspace_id="workspace-1",
            restricted=False,
        )
        evidence = replace(
            evidence,
            fields_set_aside=fields,
            original_source_requests=requests,
            comparison_fields=ComparisonFieldCoverage(
                "sha256:" + sha256(manifest.read_bytes()).hexdigest(),
                payload["semantic_hash"],
            ),
        )
        return manifest, populate_workbook_original_values(evidence, fixture.artifacts)

    def test_all_reference_codes_keep_one_literal_finding_after_hydration(self):
        for code in sorted(COMPARISON_RELATIONSHIP_CODES):
            with self.subTest(code=code):
                manifest, evidence = self.saved(code)
                path = write_review_workbook(
                    manifest, manifest.parent / "covered.xlsx", review_evidence=evidence
                )
                again = write_review_workbook(
                    manifest, manifest.parent / "again.xlsx", review_evidence=evidence
                )
                with (
                    closing(load_workbook(path)) as workbook,
                    closing(load_workbook(again)) as recreated,
                ):
                    sheet = workbook["Fields set aside"]
                    self.assertEqual(sheet["G4"].value, "1 saved findings")
                    self.assertEqual(sheet["A4"].value, "1 identified source cell")
                    self.assertEqual(sheet["E7"].value, "=1+2")
                    self.assertEqual(sheet["E7"].data_type, "s")
                    self.assertNotIn("Unavailable", str(tuple(sheet.values)))
                    self.assertEqual(
                        tuple(sheet.values), tuple(recreated["Fields set aside"].values)
                    )

    def test_same_frozen_input_cannot_reuse_coverage_for_changed_manifest_bytes(self):
        manifest, evidence = self.saved()
        before = json.loads(manifest.read_bytes())["preflight_evidence"][
            "frozen_input_hash"
        ]
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        self.assertEqual(
            json.loads(manifest.read_bytes())["preflight_evidence"][
                "frozen_input_hash"
            ],
            before,
        )
        output = manifest.parent / "changed.xlsx"
        with self.assertRaisesRegex(
            ReportGenerationError, "does not match this saved comparison"
        ):
            write_review_workbook(manifest, output, review_evidence=evidence)
        self.assertFalse(output.exists())

    def test_result_hash_is_checked_independently_of_matching_manifest_bytes(self):
        manifest, evidence = self.saved()
        altered = replace(
            evidence,
            comparison_fields=replace(
                evidence.comparison_fields,
                result_hash="sha256:" + "f" * 64,
            ),
        )
        with self.assertRaisesRegex(
            ReportGenerationError, "does not match this saved comparison"
        ):
            write_review_workbook(
                manifest, manifest.parent / "wrong-result.xlsx", review_evidence=altered
            )

    def test_coverage_cannot_suppress_reference_findings_without_field_evidence(self):
        manifest, evidence = self.saved()
        altered = replace(evidence, fields_set_aside=None)
        with self.assertRaisesRegex(
            ReportGenerationError, "does not match this saved comparison"
        ):
            write_review_workbook(
                manifest,
                manifest.parent / "missing-fields.xlsx",
                review_evidence=altered,
            )

    def test_restricted_fallback_keeps_withheld_state_without_comparison_capture(self):
        manifest, evidence = self.saved()
        fields = replace(
            evidence.fields_set_aside, findings=(), source_values_restricted=True
        )
        restricted = replace(evidence, fields_set_aside=fields, comparison_fields=None)
        path = write_review_workbook(
            manifest, manifest.parent / "restricted.xlsx", review_evidence=restricted
        )
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["G4"].value, "1 saved findings")
            self.assertEqual(sheet["E7"].value, "Withheld")
            self.assertIn("withheld", sheet["A2"].value)
            self.assertNotIn("=1+2", str(tuple(sheet.values)))

    def test_replaced_non_object_manifest_has_an_actionable_export_error(self):
        manifest, evidence = self.saved()
        manifest.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(
            ReportGenerationError, "readiness manifest is invalid"
        ):
            write_review_workbook(
                manifest, manifest.parent / "invalid.xlsx", review_evidence=evidence
            )
