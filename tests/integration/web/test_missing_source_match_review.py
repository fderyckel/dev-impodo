"""Keep missing source matches distinct from migration blockers in saved review."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.preparation.quality import (
    QualityCauseCount,
    QualityCauseImpact,
    QualityDisposition,
    QualityIssue,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityReviewItem,
    QualityReviewPage,
    QualityRowResult,
    QualityRuleFamily,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.presenters.quality_review import quality_record_details
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.hierarchy_bom import bom_workspace
from tests.support.database_probe import count_duckdb_statements


class MissingSourceMatchReviewTests(ProjectSetupBrowserTestCase):
    def test_summary_and_both_saved_cause_links_explain_the_same_missing_match(self):
        for major in (19, 20):
            with self.subTest(odoo_major=major):
                self.check_review(major)

    def check_review(self, major):
        workspace_id, _, _ = self._mapping_ready_workspace(
            scalar_field_count=0,
            odoo_major=major,
            connection_mode=OdooConnectionMode.REMOTE,
        )
        context = self.app.state.context
        staging_id, quality_id = str(uuid4()), str(uuid4())
        row_id = "sha256:" + "a" * 64
        cause_key = "sha256:" + "b" * 64
        duplicate_key = "sha256:" + "c" * 64
        invalid = QualityIssue(
            issue_id="sha256:" + "d" * 64,
            rule_id="sha256:" + "e" * 64,
            family=QualityRuleFamily.BOUNDED_VALUES,
            reason_code="SOURCE_IDENTITY_INVALID",
            message="required value is empty",
            dataset="suppliers",
            row_id=row_id,
            source_row=695,
            affected_fields=("busrel",),
            policy=QualityOutcomePolicy.QUARANTINE,
            owner_role=QualityOwnerRole.DATA_MANAGER,
            owner_label="Data manager",
        )
        duplicate = replace(
            invalid,
            issue_id="sha256:" + "f" * 64,
            reason_code="SOURCE_IDENTITY_DUPLICATE",
            affected_fields=(),
            message="source identity (None,) occurs 150 times",
        )
        cause = QualityCauseCount(
            "suppliers",
            invalid.reason_code,
            invalid.message,
            invalid.affected_fields,
            150,
            family=invalid.family,
            policy=invalid.policy,
            owner_label=invalid.owner_label,
            cause_key=cause_key,
            missing_match_table="related_accounts",
            missing_match_field_label="BusRelAccount",
        )
        duplicate_cause = replace(
            cause,
            reason_code=duplicate.reason_code,
            affected_fields=(),
            cause_key=duplicate_key,
            missing_match_table="",
            missing_match_field_label="",
            explained_by_cause_key=cause_key,
        )
        item = QualityReviewItem(
            row=QualityRowResult(
                row_id,
                "suppliers",
                695,
                "Source row 695",
                QualityDisposition.BLOCKED,
                QualityDisposition.QUARANTINED,
                (invalid.issue_id, duplicate.issue_id),
                False,
            ),
            issues=(invalid, duplicate),
            cause_impact=QualityCauseImpact.DIRECT,
            prepared_row=SimpleNamespace(
                target_model="res.partner",
                proposed_values={"name": "Acme & Sons", "vat": "BE123"},
                references={},
                source_identity=(None,),
            ),
            prepared_field_labels=(("name", "Name"), ("vat", "Tax ID")),
        )
        review = QualityReviewPage((item,), 150, 1, 8, (cause, duplicate_cause))
        quality = SimpleNamespace(
            run_id=quality_id,
            content_hash="hash",
            staging_run_id=staging_id,
            staging_content_hash="hash",
            ruleset_hash="hash",
            ready_count=1230,
            quarantined_count=150,
            excluded_count=6609,
            blocked_count=0,
            review_count=0,
        )
        staging = MagicMock(run_id=staging_id, datasets=())
        with (
            patch.object(context.preflight, "current_staging", return_value=staging),
            patch.object(context.preflight, "current_report", return_value=None),
            patch.object(context.quality, "current_summary", return_value=quality),
            patch.object(
                context.normalization,
                "current_summary",
                return_value=SimpleNamespace(frozen=True),
            ),
            patch.object(
                context.queries, "get_quality_review_page", return_value=review
            ),
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/summary?quality_status=quarantined"
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('class="notice" id="migration-issues"', response.text)
        self.assertIn(
            "150 suppliers have no matching row in related_accounts", response.text
        )
        self.assertIn("continue comparing 1230 included records", response.text)
        self.assertNotIn("Source Identity Duplicate", response.text)
        self.assertEqual(response.text.count("<h3>No matching source row</h3>"), 1)
        self.assertIn("Name:</strong> Acme &amp; Sons", response.text)
        self.assertIn("Tax ID:</strong> BE123", response.text)
        self.assertIn("Record identifier:</strong> Missing", response.text)
        self.assertIn("BusRelAccount is empty", response.text)
        self.assertLessEqual(timings.connection_count, 8)
        self.assertEqual(timings.lock_retry_count, 0)

        snapshot = context.navigation.get_for_workspace(
            workspace_id, actor=resolve_local_actor(context)
        )
        snapshot = replace(
            snapshot,
            facts=replace(
                snapshot.facts,
                staging_run_id=staging_id,
                quality_run_id=quality_id,
                quality_staging_run_id=staging_id,
            ),
        )
        for selected in (cause, duplicate_cause):
            with (
                patch.object(
                    context.navigation, "get_for_workspace", return_value=snapshot
                ),
                patch.object(
                    context.queries,
                    "get_quality_cause_review_page",
                    return_value=replace(review, selected_cause=selected),
                ),
            ):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/summary/quality-causes/{selected.cause_key}?page=2"
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("<h1>No matching source row</h1>", response.text)
            self.assertIn("does not block comparison", response.text)
            self.assertIn("Name:</strong> Acme &amp; Sons", response.text)
            self.assertIn("SOURCE_IDENTITY_DUPLICATE", response.text)


class PreparedRecordValueTests(HierarchyWorkflowFixture):
    def test_stored_matching_values_survive_reopening(self):
        from impodo.adapters.duckdb.preparation_session_repository import (
            PreparationSessionRepository,
        )
        from impodo.adapters.duckdb.quality_review_projection import (
            projected_encoded_rows_sql,
        )

        for major in (19, 20):
            with self.subTest(odoo_major=major):
                workspace_id, _ = self.hierarchy(major=major, mode="LOCAL")
                _, quality = self.prepare(workspace_id)
                for dataset_name in ("categories", "products"):
                    with (
                        patch.object(
                            PreparationSessionRepository,
                            "iter_direct_encoded_batches",
                            side_effect=AssertionError("whole-run read"),
                        ),
                        patch(
                            "impodo.adapters.duckdb.quality_review_projection.projected_encoded_rows_sql",
                            wraps=projected_encoded_rows_sql,
                        ) as query,
                        collect_duckdb_request_timings() as timings,
                        count_duckdb_statements() as statements,
                    ):
                        page = self.context.quality.quality.get_quality_review_page(
                            workspace_id,
                            quality.run_id,
                            status="ready",
                            dataset=dataset_name,
                            page=2,
                            page_size=1,
                        )
                    self.assertEqual(len(page.items), 1)
                    item = page.items[0]
                    self.assertEqual(item.prepared_row.row_id, item.row.row_id)
                    expected_name = (
                        dict(item.prepared_identity_values).get("name")
                        or item.prepared_row.proposed_values["name"]
                    )
                    self.assertIn(
                        (
                            dict(item.prepared_field_labels)["name"],
                            expected_name,
                        ),
                        quality_record_details((item,), None)[item.row.row_id],
                    )
                    self.assertIn("name", dict(item.prepared_field_labels))
                    query.assert_not_called()
                    # A standalone repository read resolves registry identity,
                    # verifies workspace linkage, and reads the engine owner.
                    self.assertEqual(timings.connection_count, 3)
                    self.assertEqual(timings.schema_check_count, 2)
                    self.assertEqual(timings.lock_retry_count, 0)
                    # Nine admission/schema statements, eight review reads,
                    # and at most one native projection binding read.
                    self.assertLessEqual(len(statements), 18)
                    self.assertEqual(
                        sum("duckdb_columns()" in sql for sql in statements), 2
                    )
                previous = page.items[0].prepared_row
                previous_identity_values = page.items[0].prepared_identity_values
                self.reopen()
                reopened = self.context.quality.quality.get_quality_review_page(
                    workspace_id,
                    quality.run_id,
                    status="ready",
                    dataset="products",
                    page=2,
                    page_size=1,
                )
                self.assertEqual(reopened.items[0].prepared_row, previous)
                self.assertEqual(
                    reopened.items[0].prepared_identity_values, previous_identity_values
                )
                self.assertEqual(
                    self.context.quality.current_summary(workspace_id).content_hash,
                    quality.content_hash,
                )

    def test_native_values_use_only_requested_ordinals_and_survive_reopening(self):
        from impodo.adapters.duckdb.preparation_session_repository import (
            PreparationSessionRepository,
        )
        from impodo.adapters.duckdb.quality_review_projection import (
            projected_encoded_rows_sql,
        )

        for major in (19, 20):
            with self.subTest(odoo_major=major):
                workspace_id, _, _ = bom_workspace(self, major=major)
                _, quality = self.prepare(workspace_id)
                for number in (1, 2):
                    with (
                        patch.object(
                            PreparationSessionRepository,
                            "iter_direct_encoded_batches",
                            side_effect=AssertionError("whole-run read"),
                        ),
                        patch(
                            "impodo.adapters.duckdb.quality_review_projection.projected_encoded_rows_sql",
                            wraps=projected_encoded_rows_sql,
                        ) as query,
                        collect_duckdb_request_timings() as timings,
                        count_duckdb_statements() as statements,
                    ):
                        page = self.context.quality.quality.get_quality_review_page(
                            workspace_id,
                            quality.run_id,
                            status="ready",
                            dataset="components",
                            page=number,
                            page_size=1,
                        )
                    item = page.items[0]
                    self.assertTrue(item.prepared_row.proposed_values["name"])
                    self.assertTrue(dict(item.prepared_identity_values)["default_code"])
                    query.assert_called_once()
                    self.assertEqual(
                        len(query.call_args.kwargs["prepared_ordinals"]), 1
                    )
                    # Registry, workspace linkage and engine; two schema contracts.
                    self.assertEqual(timings.connection_count, 3)
                    self.assertEqual(timings.schema_check_count, 2)
                    self.assertEqual(timings.lock_retry_count, 0)
                    # Nine admission/schema statements plus nine bounded reads.
                    self.assertLessEqual(len(statements), 18)
                saved = page.items[0]
                self.reopen()
                reopened = self.context.quality.quality.get_quality_review_page(
                    workspace_id,
                    quality.run_id,
                    status="ready",
                    dataset="components",
                    page=2,
                    page_size=1,
                )
                self.assertEqual(reopened.items[0].prepared_row, saved.prepared_row)
                self.assertEqual(
                    reopened.items[0].prepared_identity_values,
                    saved.prepared_identity_values,
                )
