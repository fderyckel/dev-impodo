"""Export complete correction worklists before readiness across supported targets."""

from dataclasses import replace
from io import BytesIO
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.review.attention_export import (
    verify_attention_export_current,
)
from impodo.domain.preflight.attention_projection import project_attention_export
from impodo.domain.preparation.quality import QualityDisposition, QualityOutcomePolicy
from impodo.domain.preparation.staging_contracts import StagingDisposition
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.workspace.workbench import SourceMode
from tests.integration.web.test_mapping_form_roundtrip import _RenderedControls
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_cases import CATEGORY_ROWS
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


def workbook_rows(response):
    workbook = load_workbook(BytesIO(response.content), data_only=False)
    try:
        return {
            sheet.title: tuple(sheet.iter_rows(min_row=4, values_only=True))
            for sheet in workbook.worksheets
        }
    finally:
        workbook.close()


class AttentionExportTests(HierarchyWorkflowFixture):
    def test_export_counts_match_the_workbook_without_generating_excel(self):
        workspace_id, _ = self.hierarchy(
            major=20,
            mode="LOCAL",
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C400", "", "")),
        )
        self.prepare(workspace_id)
        prefix = f"/workspaces/{workspace_id}/summary"
        with (
            patch(
                "impodo.web.routers.attention_export.build_attention_workbook"
            ) as build,
            patch(
                "impodo.web.routers.attention_export.read_attention_sources"
            ) as sources,
            patch.object(
                self.context,
                "readiness_reader",
                side_effect=AssertionError("Export contacted Odoo"),
            ),
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            response = self.client.get(prefix + "/attention/counts")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        build.assert_not_called()
        sources.assert_not_called()
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertLessEqual(len(statements), 200)
        overview = dict(
            workbook_rows(self.client.get(prefix + "/attention.xlsx"))["Overview"]
        )
        self.assertEqual(
            {key: response.json()[key] for key in ("affected_records", "findings")},
            {
                "affected_records": int(overview["Affected prepared records"]),
                "findings": int(overview["Findings"]),
            },
        )
        findings = workbook_rows(self.client.get(prefix + "/attention.xlsx"))[
            "Needs attention"
        ]
        for problem in response.json()["problems"]:
            self.assertTrue(
                any(
                    row[9] == problem["problem"]
                    and row[12] == problem["action"]
                    and row[13] == problem["correction"]
                    for row in findings
                )
            )
        stale = self.client.get(prefix + "/attention/counts?quality=old-run")
        self.assertEqual(stale.status_code, 409)
        self.assertIn("saved rows or checks changed", stale.json()["detail"])

    def test_data_check_exclusions_remain_in_the_worklist_and_filters_do_not(self):
        workspace_id, revision = self.hierarchy(
            major=20,
            mode="LOCAL",
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C400", "", "")),
        )
        staging, _ = self.prepare(workspace_id)
        quality = self.context.quality.current_run(workspace_id)
        header = self.context.preflight.attention_export(workspace_id, actor=self.actor)
        row = next(row for row in staging.rows if row.source_identity == ("C400",))
        quality = replace(
            quality,
            issues=tuple(
                replace(issue, policy=QualityOutcomePolicy.EXCLUDE)
                if issue.row_id == row.row_id
                else issue
                for issue in quality.issues
            ),
            row_results=tuple(
                replace(
                    result,
                    base_disposition=QualityDisposition.EXCLUDED,
                    effective_disposition=QualityDisposition.EXCLUDED,
                )
                if result.row_id == row.row_id
                else result
                for result in quality.row_results
            ),
        )
        arguments = (
            header,
            staging,
            quality,
            revision.definition,
            self.context.queries.get_mapping_source_selection(workspace_id),
            self.context.queries.get_odoo_schema_catalog(workspace_id),
            {},
        )
        exported = project_attention_export(*arguments)
        self.assertIn(row.row_id, {finding.row_id for finding in exported.findings})
        self.assertEqual(exported.filtered_count, 0)
        filtered = project_attention_export(
            *arguments,
            prepared_rows=tuple(
                replace(item, disposition=StagingDisposition.EXCLUDED)
                if item.row_id == row.row_id
                else item
                for item in staging.rows
            ),
        )
        self.assertNotIn(row.row_id, {finding.row_id for finding in filtered.findings})
        self.assertEqual(filtered.filtered_count, 1)

    def test_informational_comparison_findings_do_not_create_corrections(self):
        workspace_id, revision = self.hierarchy(
            major=20, mode="LOCAL", source_shape="supplied"
        )
        staging, _ = self.prepare(workspace_id)
        header = self.context.preflight.attention_export(workspace_id, actor=self.actor)
        row = staging.rows[0]
        manifest = {
            "decisions": [
                {
                    "source_trace_id": row.row_id,
                    "dataset": row.dataset,
                    "classification": "UNCHANGED",
                    "issues": [
                        {
                            "code": "INFORMATION",
                            "message": "An ordinary transformation.",
                            "severity": "info",
                        },
                    ],
                }
            ],
            "source_issues": [
                {
                    "dataset": row.dataset,
                    "row": row.source_row,
                    "code": "INFORMATION",
                    "message": "An ordinary transformation.",
                    "severity": "info",
                }
            ],
        }
        exported = project_attention_export(
            header,
            staging,
            self.context.quality.current_run(workspace_id),
            revision.definition,
            self.context.queries.get_mapping_source_selection(workspace_id),
            self.context.queries.get_odoo_schema_catalog(workspace_id),
            manifest,
        )
        self.assertNotIn("INFORMATION", {finding.code for finding in exported.findings})

    def test_missing_relationship_explanation_includes_a_value_from_matching_scope(
        self,
    ):
        workspace_id, revision = self.hierarchy(
            major=20, mode="LOCAL", source_shape="supplied"
        )
        staging, _ = self.prepare(workspace_id)
        quality = self.context.quality.current_run(workspace_id)
        row = next(
            row
            for row in staging.rows
            if row.dataset == "categories" and row.source_identity == ("C110",)
        )
        header = self.context.preflight.attention_export(workspace_id, actor=self.actor)
        manifest = {
            "decisions": [
                {
                    "source_trace_id": row.row_id,
                    "dataset": row.dataset,
                    "classification": "BLOCKED",
                    "issues": [
                        {
                            "code": "REFERENCE_NOT_FOUND",
                            "field": "parent_id",
                            "severity": "error",
                        }
                    ],
                }
            ]
        }
        export = project_attention_export(
            header,
            staging,
            quality,
            revision.definition,
            self.context.queries.get_mapping_source_selection(workspace_id),
            self.context.queries.get_odoo_schema_catalog(workspace_id),
            manifest,
        )
        self.assertIn("C100", export.findings[0].explanation)
        self.assertNotIn("(empty)", export.findings[0].explanation)

    def test_load_expectation_findings_have_whole_load_scope_and_refresh(self):
        workspace_id, _ = self.hierarchy(major=20, mode="LOCAL")
        self.prepare(workspace_id)
        self.approve(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        url = f"/workspaces/{workspace_id}/summary/attention.xlsx"
        before = self.client.get(url)
        self.assertEqual(before.status_code, 200)
        findings = workbook_rows(before)["Needs attention"]
        expectation = next(
            row for row in findings if row[-1] == "BUSINESS_EXPECTATIONS_NEED_REVIEW"
        )
        self.assertEqual(expectation[3], "Applies to the whole load")
        self.assertIsNone(expectation[4])
        self.expect_business_writes(workspace_id)
        after = self.client.get(url)
        self.assertEqual(after.status_code, 200)
        self.assertFalse(
            any(
                row[-1] == "BUSINESS_EXPECTATIONS_NEED_REVIEW"
                for row in workbook_rows(after)["Needs attention"]
            )
        )

    def test_accepted_set_asides_retain_the_original_correction_worklist(self):
        workspace_id, _ = self.hierarchy(
            major=20,
            mode="LOCAL",
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C400", "", "")),
        )
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        target.records[("product.category", 900)] = {
            "name": "Machines",
            "parent_id": False,
        }
        self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        prefix = f"/workspaces/{workspace_id}/summary"
        original = workbook_rows(self.client.get(prefix + "/attention.xlsx"))
        preview = self._post(
            prefix + "/deferred-groups/preview",
            {"csrf_token": self.csrf, "select_all": "1"},
        )
        self.assertEqual(preview.status_code, 200)
        controls = _RenderedControls()
        controls.feed(preview.text)
        values = dict(controls.entries)
        accepted = self._post(
            prefix + "/deferred-groups/accept",
            {
                "csrf_token": self.csrf,
                "select_all": "1",
                "comparison_id": values["comparison_id"],
                "preview_hash": values["preview_hash"],
            },
        )
        self.assertEqual(accepted.status_code, 303)
        exported = self.client.get(prefix + "/attention.xlsx")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            workbook_rows(exported)["Needs attention"], original["Needs attention"]
        )

    def test_permissions_are_checked_before_correction_evidence_is_read(self):
        workspace_id, _ = self.hierarchy(major=20, mode="LOCAL")
        self.prepare(workspace_id)
        original = self.context.preflight.authorization.require

        def require(actor, capability, **kwargs):
            if capability is Capability.PROTECTED_EVIDENCE_READ:
                raise AuthorizationError("Correction export is not permitted")
            return original(actor, capability, **kwargs)

        with (
            patch.object(
                self.context.preflight.authorization, "require", side_effect=require
            ),
            patch.object(
                self.context.preflight.staging, "get_canonical_staging_run"
            ) as read_rows,
        ):
            for suffix in ("/attention.xlsx", "/attention/counts"):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/summary" + suffix
                )
                self.assertEqual(response.status_code, 403)
        read_rows.assert_not_called()

    def test_changes_during_generation_do_not_return_stale_workbook_bytes(self):
        workspace_id, _ = self.hierarchy(major=20, mode="LOCAL")
        self.prepare(workspace_id)

        def changed(service, export, *, actor):
            with patch.object(
                service.quality, "get_current_quality_summary", return_value=None
            ):
                verify_attention_export_current(service, export, actor=actor)

        with patch(
            "impodo.web.routers.attention_export.verify_attention_export_current",
            side_effect=changed,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/summary/attention.xlsx"
            )
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("content-disposition", response.headers)
        self.assertIn("saved rows or checks changed", response.text)

    def test_preparation_and_blocked_comparison_for_both_majors_and_access_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self.exercise(major, mode)

    def exercise(self, major, mode):
        workspace_id, _ = self.hierarchy(
            major=major,
            mode=mode,
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C400", "", "")),
        )
        _, quality = self.prepare(workspace_id)
        prefix = f"/workspaces/{workspace_id}/summary"
        page = self.client.get(prefix + "?quality_status=ready&quality_page=2")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Download rows needing attention", page.text)
        with (
            patch.object(
                self.context,
                "readiness_reader",
                side_effect=AssertionError("Export contacted Odoo"),
            ),
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            download = self.client.get(prefix + "/attention.xlsx?status=ready&page=999")
        self.assertEqual(
            download.status_code,
            200,
            download.text[:300] if download.status_code != 200 else "",
        )
        self.assertIn("needs-attention.xlsx", download.headers["content-disposition"])
        self.assertEqual(download.headers["cache-control"], "no-store")
        rows = workbook_rows(download)
        overview = dict(rows["Overview"])
        self.assertEqual(overview["Odoo check"], "Odoo comparison not completed")
        self.assertEqual(int(overview["Findings"]), len(rows["Needs attention"]))
        self.assertGreater(len(rows["Needs attention"]), 0)
        self.assertTrue(any(title.startswith("Data ·") for title in rows))
        self.assertTrue(
            any("C400" in str(row) for title, values in rows.items() for row in values)
        )
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), 200)

        stale = self.client.get(prefix + "/attention.xlsx?quality=old-run")
        self.assertEqual(stale.status_code, 409)
        self.assertIn("saved rows or checks changed", stale.text)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        target.records[("product.category", 900)] = {
            "name": "Machines",
            "parent_id": False,
        }
        report = self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        self.assertNotEqual(report.status, "READY")
        requests = len(target.requests)
        download = self.client.get(
            prefix + f"/attention.xlsx?comparison={report.run_id}"
        )
        self.assertEqual(
            download.status_code,
            200,
            download.text[:300] if download.status_code != 200 else "",
        )
        rows = workbook_rows(download)
        self.assertEqual(
            dict(rows["Overview"])["Odoo check"], "Odoo comparison completed"
        )
        self.assertTrue(
            any("Several Odoo records" in str(row) for row in rows["Needs attention"])
        )
        self.assertEqual(len(target.requests), requests)
        self.assertEqual(target.writes, [])
        self.assertEqual(
            self.context.quality.current_summary(workspace_id).run_id, quality.run_id
        )

    def test_duplicate_peers_and_inherited_rows_are_complete(self):
        workspace_id, _ = self.hierarchy(
            major=20,
            mode="LOCAL",
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C999", "Pumps", "C100")),
        )
        self.prepare(workspace_id)
        download = self.client.get(f"/workspaces/{workspace_id}/summary/attention.xlsx")
        self.assertEqual(download.status_code, 200)
        rows = workbook_rows(download)
        collisions = [
            row
            for row in rows["Needs attention"]
            if row[-1] == "POST_TRANSFORM_IDENTITY_COLLISION"
        ]
        self.assertEqual(len(collisions), 2)
        for row in collisions:
            self.assertIn("C999", row[11])
            self.assertIn("C110", row[11])
            self.assertIn("Name", row[6])
            self.assertIn("complete Odoo match", row[10])
        self.assertIn("Duplicate groups", rows)
        self.assertEqual(len(rows["Duplicate groups"]), 2)
        self.assertGreater(
            int(dict(rows["Overview"])["Records held back by a linked record"]), 0
        )

    def test_odoo_source_values_are_removed_before_artifact_generation(self):
        workspace_id, _ = self.hierarchy(
            major=19,
            mode="LOCAL",
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C999", "", "")),
        )
        self.prepare(workspace_id)
        original_get = self.context.preflight.workspaces.get

        def protected_workspace(identifier):
            return replace(original_get(identifier), source_mode=SourceMode.ODOO)

        # The same source-side evidence exercises the portable value restriction.
        with patch.object(
            self.context.preflight.workspaces, "get", side_effect=protected_workspace
        ):
            download = self.client.get(
                f"/workspaces/{workspace_id}/summary/attention.xlsx"
            )
        self.assertEqual(download.status_code, 200)
        rows = workbook_rows(download)
        display = str(rows)
        self.assertIn("Restricted", display)
        self.assertNotIn("C999", display)
        self.assertNotIn("Machines", display)
