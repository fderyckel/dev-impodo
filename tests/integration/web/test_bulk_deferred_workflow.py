"""Review and confirm every problem group through authenticated Final review."""

from io import BytesIO
from dataclasses import replace
from uuid import uuid4
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.database_probe import count_duckdb_statements
from impodo.domain.mapping.contracts import (
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from tests.integration.web.test_mapping_form_roundtrip import _RenderedControls
from tests.support.hierarchy_cases import CATEGORY_ROWS
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class BulkDeferredWorkflowTests(HierarchyWorkflowFixture):
    def test_bulk_review_and_excel_for_both_odoo_majors_and_access_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._review_and_export(major, mode)

    def _review_and_export(self, major, mode):
        workspace_id, revision = self.hierarchy(
            major=major, mode=mode, source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C400", "", "")),
        )
        self.submit(
            workspace_id,
            tuple(
                replace(mapping, row_inclusion=RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    included_parent_field="categ_id",
                ))
                if mapping.target_model == "product.template"
                else replace(mapping, row_inclusion=RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=(RowInclusionCondition(
                        str(uuid4()), mapping.source_identity_column_keys[0],
                        SelectionConditionOperator.NOT_EQUALS, "C300",
                    ),),
                ))
                for mapping in revision.definition.datasets
            ),
            confirm_rows=True,
        )
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(self.context.queries.get_odoo_schema_catalog(workspace_id))
        target.records[("product.category", 900)] = {"name": "Machines", "parent_id": False}
        report = self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor,
        )
        self.assertNotEqual(report.status, "READY")
        requests_after_comparison = len(target.requests)
        url = f"/workspaces/{workspace_id}/summary"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Preview all problem groups", page.text)
        prepared_review = self.client.get(url + "?quality_status=quarantined")
        self.assertEqual(prepared_review.status_code, 200)
        quality_rows = prepared_review.text.split('id="quality-rows"', 1)[1].split('</section>', 1)[0]
        self.assertIn("Excluded by filters", quality_rows)
        self.assertIn("This row did not meet your Rows to use rule.", quality_rows)
        self.assertIn("Review Rows to use", quality_rows)
        self.assertIn("Set aside for a data issue", quality_rows)
        self.assertNotIn("No target-independent problem was found.", quality_rows)
        self.assertNotIn("No action needed", quality_rows)

        with collect_duckdb_request_timings() as timings:
            preview = self._post(url + "/deferred-groups/preview", {
                "csrf_token": self.csrf, "select_all": "1",
            })
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("Set aside all problem groups", preview.text)
        self.assertIn("data-single-submit", preview.text)
        self.assertIn('data-submitting-label="Saving set-aside decision…"', preview.text)
        self.assertNotIn('name="issue_id"', preview.text)
        # Access, verification and rendering share one bounded read scope.
        self.assertLessEqual(timings.connection_count, 6)
        # Count both linkage-store and engine validation in each local scope.
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        controls = _RenderedControls()
        controls.feed(preview.text)
        values = dict(controls.entries)

        confirmation = {
            "csrf_token": self.csrf, "select_all": "1",
            "comparison_id": values["comparison_id"],
            "preview_hash": values["preview_hash"],
        }
        accept_url = url + "/deferred-groups/accept"
        with (
            collect_duckdb_request_timings() as timings,
            patch.object(
                self.context.preflight, "_full_execution_snapshot",
                wraps=self.context.preflight._full_execution_snapshot,
            ) as full_snapshot,
        ):
            accepted = self._post(accept_url, confirmation)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        full_snapshot.assert_called_once_with(workspace_id, report.run_id)
        # Verification and publication retain the foundation, workspace,
        # data-version and prepared-evidence owners in one local operation.
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        review = self.context.preflight.deferred_scope_review(workspace_id)
        self.assertTrue(review.preview.ready_with_records_set_aside)
        self.assertIsNotNone(review.decision)
        self.assertGreater(review.preview.remaining_write_count, 0)

        with (
            collect_duckdb_request_timings() as timings,
            patch.object(self.context.preflight.preflight, "save_deferred_scope_projection") as publish,
        ):
            replay = self._post(accept_url, confirmation)
        self.assertEqual(replay.status_code, 303, replay.text)
        self.assertEqual(replay.headers["location"], url)
        publish.assert_not_called()
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(
            self.context.preflight.deferred_scope_review(workspace_id).decision,
            review.decision,
        )

        def evidence():
            return run_local_operation_sync(
                self.context.preflight.review_workbook_evidence, workspace_id, report.run_id,
            )

        with (
            patch("impodo.application.preflight_service.workbook_mapping_rules", return_value={}),
            collect_duckdb_request_timings() as baseline_timings,
            count_duckdb_statements() as baseline_sql,
        ):
            baseline = evidence()
        with (
            collect_duckdb_request_timings() as export_timings,
            count_duckdb_statements() as export_sql,
        ):
            detailed = evidence()
        self.assertTrue(detailed.mapping_rules)
        self.assertEqual(detailed.frozen_input_hash, baseline.frozen_input_hash)
        self.assertEqual(detailed.cell_effects, baseline.cell_effects)
        self.assertEqual(export_timings.connection_count, baseline_timings.connection_count)
        self.assertEqual(export_timings.schema_check_count, baseline_timings.schema_check_count)
        self.assertEqual(len(export_sql), len(baseline_sql))

        package = self._post(url + "/package", {"csrf_token": self.csrf})
        self.assertEqual(package.status_code, 303, package.text)
        download = self.client.get(url + "/workbook")
        self.assertEqual(download.status_code, 200, download.text[:200])
        workbook = load_workbook(BytesIO(download.content), read_only=True, data_only=True)
        self.addCleanup(workbook.close)
        overview_rows = tuple(workbook.worksheets[0].iter_rows(values_only=True))
        self.assertTrue(any("Source column" in row for row in overview_rows))
        self.assertFalse(any("Source column ID" in row for row in overview_rows))
        category_name = next(row for row in overview_rows if "product.category.name" in row)
        self.assertEqual(category_name[1], "Name")
        self.assertEqual(
            " ".join(category_name[4].split()),
            "1. Rows to use: Include a row when Key is not C300.",
        )
        omissions = tuple(workbook["Deferred issues"].iter_rows(min_row=4, values_only=True))
        self.assertEqual(len(omissions), review.preview.newly_set_aside_count + 1)
        headers = tuple(cell.value for cell in workbook["Deferred issues"][3])
        self.assertEqual(
            {row[headers.index("Stage")] for row in omissions},
            {"Prepare data", "Final review"},
        )
        self.assertEqual(
            len(tuple(workbook["Excluded by filters"].iter_rows(min_row=4))),
            2,
        )
        self.assertEqual(len(target.requests), requests_after_comparison)
        self.assertEqual(target.writes, [])
