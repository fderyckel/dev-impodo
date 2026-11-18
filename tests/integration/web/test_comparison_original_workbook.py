"""Export exact direct relationship originals from a saved comparison after restart."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.preflight_service import MANIFEST_NAME
from impodo.domain.preflight.fields_set_aside import FieldSetAsideReason
from impodo.domain.preflight.original_values import OriginalValueState
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_recipe_comparison_recovery as recipes
from tests.integration.web import test_reference_review_results as references
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.identity import resolve_local_actor


def _remove_uploaded_sources(context, workspace_id: str) -> int:
    def recorded_sources():
        return (
            context.queries.get(workspace_id),
            context.queries.get_source_selection(workspace_id),
        )

    state, selection = run_local_operation_sync(recorded_sources)
    assert selection is not None
    for source in state.source_files:
        context.preflight.artifacts.delete_source(
            selection.data_version_id, source.stored_name
        )
    return len(state.source_files)


def _export_saved_workbook(context, workspace_id: str, report, path: Path):
    evidence = run_local_operation_sync(
        context.preflight.review_workbook_evidence, workspace_id, report.run_id
    )
    evidence = populate_workbook_original_values(evidence, context.preflight.artifacts)
    with context.artifacts.materialize_report(
        workspace_id, report.run_id, MANIFEST_NAME
    ) as manifest:
        write_review_workbook(manifest, path, review_evidence=evidence)
    return evidence


class ComparisonOriginalWorkbookTests(HierarchyWorkflowFixture):
    def test_reopened_generic_comparison_preserves_originals_once_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id = references.ReferenceReviewResultsTests.configure(
                        self, major, mode
                    )
                    self.prepare(workspace_id)
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
                    target = HierarchyOdoo(schema)
                    report = run_local_operation_sync(
                        self.context.preflight.compare,
                        workspace_id,
                        reader=target.compare,
                        actor=self.actor,
                    )
                    self.assertEqual(report.blocked_count, 2)
                    self.assertEqual(
                        {item.technical_code for item in report.issue_counts},
                        {"REFERENCE_NOT_FOUND"},
                    )
                    self.assertEqual(
                        _remove_uploaded_sources(self.context, workspace_id), 1
                    )
                    self.reopen()
                    output = self.enterContext(TemporaryDirectory())
                    path = Path(output) / f"comparison-{major}-{mode}.xlsx"
                    with (
                        patch.object(
                            self.context,
                            "readiness_reader",
                            side_effect=AssertionError("Export contacted Odoo"),
                        ),
                        patch.object(
                            self.context.preflight.artifacts,
                            "materialize_source",
                            side_effect=AssertionError(
                                "Export reopened uploaded source"
                            ),
                        ),
                        patch.object(
                            self.context.preflight.artifacts,
                            "materialize_source_snapshot",
                            wraps=self.context.preflight.artifacts.materialize_source_snapshot,
                        ) as snapshots,
                    ):
                        evidence = _export_saved_workbook(
                            self.context, workspace_id, report, path
                        )
                    snapshots.assert_called_once()
                    assert (
                        evidence is not None and evidence.fields_set_aside is not None
                    )
                    coverage = evidence.comparison_fields
                    assert coverage is not None
                    self.assertEqual(coverage.manifest_hash, report.manifest_hash)
                    self.assertEqual(coverage.result_hash, report.result_hash)
                    findings = evidence.fields_set_aside.findings
                    self.assertEqual(len(findings), 2)
                    self.assertTrue(
                        all(finding.comparison_issue for finding in findings)
                    )
                    self.assertTrue(
                        all(
                            tuple(item.coordinate.source_key for item in finding.source_inputs)
                            == finding.source_keys
                            and len(finding.source_inputs) == 1
                            for finding in findings
                        )
                    )
                    self.assertTrue(
                        all(
                            item.value.state is OriginalValueState.PRESENT
                            for finding in findings
                            for item in finding.source_inputs
                        )
                    )
                    self.assertEqual(
                        tuple(
                            item.value.text
                            for finding in findings
                            for item in finding.source_inputs
                        ),
                        ("000745", "000746"),
                    )
                    self.assertEqual(evidence.original_source_requests, ())
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
                        self.assertEqual(len(rows), 2)
                        self.assertEqual(
                            tuple(row[0] for row in rows),
                            (FieldSetAsideReason.UNRECOGNIZED.value,) * 2,
                        )
                        self.assertEqual(
                            tuple(row[4] for row in rows), ("000745", "000746")
                        )
                        self.assertTrue(
                            all(sheet.cell(row, 5).data_type == "s" for row in (7, 8))
                        )
                        self.assertEqual(sheet["A4"].value, "2 identified source cells")
                        self.assertEqual(sheet["G4"].value, "2 saved findings")
                        self.assertNotIn(
                            "no available original value", sheet["A2"].value
                        )
                        self.assertNotIn("Unavailable", tuple(row[4] for row in rows))
                    current = run_local_operation_sync(
                        self.context.preflight.current_report, workspace_id
                    )
                    self.assertEqual(current.run_id, report.run_id)
                    self.assertEqual(current.result_hash, report.result_hash)
                    self.assertFalse(target.writes)


class RecipeComparisonWorkbookTests(TestCase):
    def test_existing_recipe_export_without_relationship_issues_needs_no_original_reads(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture = recipes.RecipeComparisonRecoveryBrowserTests()
                fixture.setUp()
                try:
                    application, reader = fixture._approved_review(
                        odoo_version=f"{major}.0"
                    )
                    context = fixture.context
                    report = run_local_operation_sync(
                        context.preflight.compare,
                        application.workspace_id,
                        reader=reader,
                        actor=resolve_local_actor(context),
                    )
                    self.assertEqual(report.target_odoo_version, f"{major}.0")
                    self.assertEqual(report.status, "READY")
                    with (
                        TemporaryDirectory() as temporary,
                        patch.object(
                            context,
                            "readiness_reader",
                            side_effect=AssertionError("Recipe export contacted Odoo"),
                        ),
                        patch.object(
                            context.preflight.artifacts,
                            "materialize_source",
                            side_effect=AssertionError("Recipe export reopened source"),
                        ),
                        patch.object(
                            context.preflight.artifacts,
                            "materialize_source_snapshot",
                            side_effect=AssertionError(
                                "Recipe export read unused originals"
                            ),
                        ) as snapshots,
                    ):
                        path = Path(temporary) / "recipe-review.xlsx"
                        evidence = _export_saved_workbook(
                            context, application.workspace_id, report, path
                        )
                        assert (
                            evidence is not None
                            and evidence.fields_set_aside is not None
                        )
                        self.assertIsNone(evidence.comparison_fields)
                        self.assertEqual(evidence.fields_set_aside.findings, ())
                        self.assertEqual(evidence.original_source_requests, ())
                        with closing(load_workbook(path)) as workbook:
                            self.assertEqual(
                                workbook["Fields set aside"]["G4"].value,
                                "0 saved findings",
                            )
                            rows = tuple(
                                workbook["Records to load"].iter_rows(
                                    min_row=4, values_only=True
                                )
                            )
                            self.assertTrue(any("Acme" in row for row in rows))
                            self.assertTrue(any("Beta" in row for row in rows))
                    snapshots.assert_not_called()
                finally:
                    fixture.doCleanups()
