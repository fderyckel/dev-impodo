"""Recreate prepared-reference originals from a saved generic comparison."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.domain.mapping.contracts import ReferenceKeyMapping
from impodo.domain.mapping.reference_preparation import ReferencePreparation
from impodo.domain.preflight.original_values import OriginalValueState
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_comparison_original_workbook as workbooks
from tests.integration.web import test_reference_review_results as references
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class PreparedOriginalWorkbookTests(HierarchyWorkflowFixture):
    def configure_prepared(self, major, mode):
        workspace_id = references.ReferenceReviewResultsTests.configure(
            self, major, mode
        )
        revision = self.context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        (mapping,) = revision.definition.datasets
        (source,) = self.context.queries.get_source_selection(workspace_id).datasets
        columns = {column.source_name: column.stable_key for column in source.columns}
        relationship = mapping.relationships[0]
        prepared = replace(
            relationship,
            source_column_keys=(columns["Code"],),
            resolver=replace(
                relationship.resolver,
                key_mappings=(
                    ReferenceKeyMapping(
                        columns["Code"],
                        "ref",
                        ReferencePreparation(
                            formula="concat(column_2)",
                            bindings=(("column_2", columns["Contact"]),),
                        ),
                    ),
                ),
            ),
        )
        self.submit(workspace_id, (replace(mapping, relationships=(prepared,)),))
        return workspace_id, columns

    def test_reopened_formula_comparison_exports_only_contributing_cells_without_evaluation(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id, columns = self.configure_prepared(major, mode)
                    staging, _quality = self.prepare(workspace_id)
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    target = HierarchyOdoo(
                        self.context.queries.get_odoo_schema_catalog(workspace_id)
                    )
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
                        workbooks._remove_uploaded_sources(self.context, workspace_id),
                        1,
                    )
                    self.reopen()
                    with TemporaryDirectory() as temporary:
                        exports = []
                        for attempt in (1, 2):
                            path = Path(temporary) / f"prepared-{attempt}.xlsx"
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
                                        "Export reopened upload"
                                    ),
                                ),
                                patch.object(
                                    self.context.preflight.artifacts,
                                    "materialize_source_snapshot",
                                    wraps=self.context.preflight.artifacts.materialize_source_snapshot,
                                ) as snapshots,
                                patch(
                                    "impodo.domain.staging.evaluator.prepare_reference_value",
                                    side_effect=AssertionError(
                                        "Export prepared a reference"
                                    ),
                                ),
                                patch(
                                    "impodo.domain.mapping.reference_preparation.evaluate_reference_formula",
                                    side_effect=AssertionError(
                                        "Export evaluated a formula"
                                    ),
                                ),
                            ):
                                evidence = workbooks._export_saved_workbook(
                                    self.context, workspace_id, report, path
                                )
                            snapshots.assert_called_once()
                            fields = evidence.fields_set_aside
                            self.assertEqual(len(fields.findings), 2)
                            self.assertEqual(
                                tuple(
                                    finding.source_keys for finding in fields.findings
                                ),
                                ((columns["Code"], columns["Contact"]),) * 2,
                            )
                            self.assertTrue(
                                all(
                                    tuple(
                                        item.coordinate.source_key
                                        for item in finding.source_inputs
                                    )
                                    == (columns["Contact"],)
                                    for finding in fields.findings
                                )
                            )
                            self.assertEqual(
                                tuple(
                                    item.value.text
                                    for finding in fields.findings
                                    for item in finding.source_inputs
                                ),
                                ("000745", "000746"),
                            )
                            self.assertTrue(
                                all(
                                    item.value.state is OriginalValueState.PRESENT
                                    for finding in fields.findings
                                    for item in finding.source_inputs
                                )
                            )
                            self.assertTrue(
                                all(
                                    finding.comparison_issue
                                    for finding in fields.findings
                                )
                            )
                            self.assertEqual(evidence.original_source_requests, ())
                            with closing(load_workbook(path)) as workbook:
                                sheet = workbook["Fields set aside"]
                                rows = tuple(
                                    sheet.iter_rows(min_row=7, values_only=True)
                                )
                                self.assertEqual(len(rows), 2)
                                self.assertEqual(
                                    tuple(row[3] for row in rows), ("Contact",) * 2
                                )
                                self.assertEqual(
                                    tuple(row[4] for row in rows), ("000745", "000746")
                                )
                                self.assertEqual(
                                    sheet["A4"].value, "2 identified source cells"
                                )
                                self.assertEqual(sheet["G4"].value, "2 saved findings")
                                self.assertTrue(
                                    all(
                                        sheet.cell(row, 5).data_type == "s"
                                        for row in (7, 8)
                                    )
                                )
                                self.assertNotIn("N-01", str(rows))
                                self.assertNotIn("N-02", str(rows))
                                exports.append(rows)
                        self.assertEqual(exports[0], exports[1])
                    current = run_local_operation_sync(
                        self.context.preflight.current_report, workspace_id
                    )
                    self.assertEqual(current.run_id, report.run_id)
                    self.assertEqual(current.result_hash, report.result_hash)
                    current_staging = (
                        self.context.preparation.staging.get_current_staging_summary(
                            workspace_id
                        )
                    )
                    self.assertEqual(current_staging.content_hash, staging.content_hash)
                    self.assertFalse(target.writes)
