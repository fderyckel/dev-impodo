"""Prove complete BoM cause accounting, bounded review, and saved correction."""

from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.shared.access import Actor, ActorIdentity
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.hierarchy_bom import bom_workspace
from tests.support.hierarchy_correction import correct_bom_through_browser
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.database_probe import count_duckdb_statements


class HierarchyCauseWorkflowTests(HierarchyWorkflowFixture):
    def test_missing_component_sets_aside_four_records_and_correction_preserves_history(
        self,
    ):
        workspace_id, _revision, columns = bom_workspace(self)
        _staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.quarantined_count, 4)
        self.assertEqual(quality.blocked_count, 0)
        self.assertEqual(quality.ready_count, 11)
        repository = self.context.quality.quality
        page = repository.get_quality_review_page(
            workspace_id, quality.run_id, include_run_cause_counts=True
        )
        cause = next(
            cause
            for cause in page.run_cause_counts
            if cause.reason_code == "INCOMING_RELATIONSHIP_MISSING"
        )
        self.assertEqual(
            (
                cause.affected_record_count,
                cause.affected_parent_count,
                cause.affected_sibling_count,
            ),
            (1, 1, 2),
            [
                (
                    issue.reason_code,
                    issue.row_id,
                    issue.caused_by_row_id,
                    issue.dependency_impact,
                )
                for issue in self.context.quality.current_run(workspace_id).issues
            ],
        )
        members = repository.get_quality_cause_review_page(
            workspace_id, quality.run_id, cause.cause_key, page_size=2
        )
        self.assertEqual(members.matching_count, 4)
        second = repository.get_quality_cause_review_page(
            workspace_id, quality.run_id, cause.cause_key, page=2, page_size=2
        )
        self.assertEqual(
            len({item.row.row_id for item in (*members.items, *second.items)}), 4
        )
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/summary/quality-causes/{cause.cause_key}"
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Records affected by this issue", response.text)
        # Two admission owners precede the retained page's registry, workspace,
        # engine, and Data version owners. The Data version read resolves the
        # saved source column names; the four store contracts validate once.
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        # Prepared records add four bounded reads to the existing page budget.
        prepared_reads = (
            "SELECT row_id, row_json FROM canonical_staging_row",
            "SELECT projection.projection_json, manifest.manifest_json,",
            "SELECT json_extract_string(model.value, '$.name'),",
            "SELECT revision.revision_json FROM canonical_staging_run",
        )
        normalized = [" ".join(statement.split()) for statement in statements]
        for prefix in prepared_reads:
            self.assertEqual(
                sum(statement.startswith(prefix) for statement in normalized),
                1,
                prefix,
            )
        self.assertLessEqual(len(statements), 59 + len(prepared_reads))
        self.assertEqual(timings.lock_retry_count, 0)
        historical = repository.get_quality_run(workspace_id, quality.run_id).to_json()
        correct_bom_through_browser(self, workspace_id, columns)
        _new_staging, corrected = self.prepare(workspace_id)
        self.assertNotEqual(corrected.run_id, quality.run_id)
        self.assertEqual(corrected.ready_count, 15)
        self.assertEqual(corrected.quarantined_count, 0)
        self.assertEqual(
            repository.get_quality_run(workspace_id, quality.run_id).to_json(),
            historical,
        )
        with self.assertRaisesRegex(WorkspaceError, "not available"):
            repository.get_quality_cause_review_page(
                workspace_id, corrected.run_id, cause.cause_key
            )
        stale = self.client.get(
            f"/workspaces/{workspace_id}/summary/quality-causes/{cause.cause_key}"
        )
        self.assertEqual(stale.status_code, 404)
        self.reopen()
        self.assertEqual(
            self.context.quality.quality.get_quality_run(
                workspace_id, quality.run_id
            ).to_json(),
            historical,
        )

    def test_two_causes_keep_both_findings_without_multiplying_the_record_total(self):
        workspace_id, _revision, columns = bom_workspace(self, two_problems=True)
        _staging, quality = self.prepare(workspace_id)
        repository = self.context.quality.quality
        page = repository.get_quality_review_page(
            workspace_id, quality.run_id, include_run_cause_counts=True
        )
        causes = [
            cause
            for cause in page.run_cause_counts
            if cause.reason_code
            in {"SOURCE_TYPE_INVALID", "INCOMING_RELATIONSHIP_MISSING"}
        ]
        self.assertEqual(len(causes), 2)
        closures = [
            repository.get_quality_cause_review_page(
                workspace_id, quality.run_id, cause.cause_key
            )
            for cause in causes
        ]
        self.assertEqual([closure.matching_count for closure in closures], [4, 4])
        self.assertEqual(
            len({item.row.row_id for closure in closures for item in closure.items}), 4
        )
        self.assertEqual(quality.quarantined_count, 4)
        historical = repository.get_quality_run(workspace_id, quality.run_id).to_json()
        correct_bom_through_browser(self, workspace_id, columns, two_problems=True)
        _staging, corrected = self.prepare(workspace_id)
        self.assertEqual((corrected.ready_count, corrected.quarantined_count), (15, 0))
        self.assertEqual(
            repository.get_quality_run(workspace_id, quality.run_id).to_json(), historical
        )

    def test_denied_actor_cannot_read_cause_evidence(self):
        workspace_id, _, _ = bom_workspace(self)
        self.prepare(workspace_id)
        denied = Actor(
            ActorIdentity("test", "no-workspace-access", "Denied operator"), frozenset()
        )
        with (
            patch.object(self.context.identity_resolver, "_actor", denied),
            patch.object(self.context.queries, "get_quality_cause_review_page") as read,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/summary/quality-causes/sha256:" + "0" * 64
            )
        self.assertIn(response.status_code, (403, 404))
        read.assert_not_called()

    def test_cause_key_from_another_workspace_is_not_available(self):
        first, _, _ = bom_workspace(self, two_problems=True)
        self.prepare(first)
        quality = self.context.quality.current_summary(first)
        page = self.context.quality.quality.get_quality_review_page(
            first, quality.run_id, include_run_cause_counts=True
        )
        cause = next(
            item for item in page.run_cause_counts if item.reason_code == "SOURCE_TYPE_INVALID"
        )
        second, _, _ = bom_workspace(self)
        self.prepare(second)
        second_quality = self.context.quality.current_summary(second)
        second_page = self.context.quality.quality.get_quality_review_page(
            second, second_quality.run_id, include_run_cause_counts=True
        )
        self.assertNotIn(cause.cause_key, {item.cause_key for item in second_page.run_cause_counts})
        response = self.client.get(
            f"/workspaces/{second}/summary/quality-causes/{cause.cause_key}"
        )
        self.assertEqual(response.status_code, 404)
