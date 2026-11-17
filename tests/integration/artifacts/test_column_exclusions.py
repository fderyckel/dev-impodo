"""Report only explicitly saved column choices, without exporting their values."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from impodo.adapters.artifacts.profile_loader import load_profile
from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.domain.compiler import compile_profile_document
from impodo.domain.preflight.fields_set_aside import field_set_aside_evidence
from tests.integration.artifacts import test_fields_set_aside_coverage as coverage
from tests.integration.artifacts.test_preflight_outputs import ROOT


class SavedColumnExclusionWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.plan = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        self.dataset = self.plan.datasets[0].name
        self.labels = {
            (self.dataset, "notes"): "Internal notes",
            (self.dataset, "other"): "Other unmapped column",
        }

    def evidence(self, exclusions):
        return field_set_aside_evidence(
            self.plan,
            (),
            self.labels,
            {},
            column_exclusions=exclusions,
        )

    def test_explicit_choice_is_one_whole_column_with_no_source_values(self):
        fields = self.evidence({self.dataset: ("notes",)})
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    manifest = coverage._empty_manifest(root, major, mode)
                    evidence = coverage._evidence(fields)
                    first = write_review_workbook(
                        manifest, root / "first.xlsx", review_evidence=evidence
                    )
                    recreated = write_review_workbook(
                        manifest, root / "again.xlsx", review_evidence=evidence
                    )
                    self.assertEqual(
                        coverage._projection(first), coverage._projection(recreated)
                    )
                    with closing(load_workbook(first)) as workbook:
                        sheet = workbook["Fields set aside"]
                        self.assertEqual(sheet["A4"].value, "0 identified source cells")
                        self.assertEqual(sheet["D4"].value, "1 whole column")
                        self.assertEqual(sheet["G4"].value, "1 saved findings")
                        self.assertEqual(sheet["A7"].value, "Intentionally excluded")
                        self.assertEqual(sheet["D7"].value, "Internal notes")
                        self.assertIsNone(sheet["E7"].value)
                        self.assertIsNone(sheet["F7"].value)
                        self.assertEqual(sheet["G7"].value, "Whole column")
                        self.assertNotIn(
                            "Other unmapped column", str(tuple(sheet.values))
                        )
                        self.assertNotIn("unmapped are not recorded", sheet["A2"].value)
                        self.assertIn("Original source values", sheet["A2"].value)

    def test_an_absent_mapping_is_not_an_intentional_exclusion(self):
        fields = self.evidence({self.dataset: ()})
        self.assertEqual(fields.findings, ())
        self.assertFalse(any("unmapped" in gap for gap in fields.coverage_gaps))
        self.assertTrue(
            any("earlier unsaved choices" in gap for gap in fields.coverage_gaps)
        )
        self.assertTrue(fields.coverage_gaps)

    def test_unavailable_exclusion_metadata_retains_the_coverage_gap(self):
        fields = field_set_aside_evidence(self.plan, (), self.labels, {})
        self.assertEqual(fields.findings, ())
        self.assertTrue(any("unmapped" in gap for gap in fields.coverage_gaps))
