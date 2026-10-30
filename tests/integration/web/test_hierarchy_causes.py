"""Prove complete BoM cause accounting, bounded review, and saved correction."""

from dataclasses import replace

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.hierarchy_bom import bom_workspace
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyCauseWorkflowTests(HierarchyWorkflowFixture):
    def test_missing_component_sets_aside_four_records_and_correction_preserves_history(
        self,
    ):
        workspace_id, revision, columns = bom_workspace(self)
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
        with collect_duckdb_request_timings() as timings:
            response = self.client.get(
                f"/workspaces/{workspace_id}/summary/quality-causes/{cause.cause_key}"
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Records behind this finding", response.text)
        # The real route includes authorizing foundation reads before the
        # retained navigation/evidence read; the mocked route tests omit them.
        self.assertLessEqual(timings.connection_count, 5)
        self.assertLessEqual(timings.schema_check_count, 5)
        self.assertEqual(timings.lock_retry_count, 0)
        historical = repository.get_quality_run(workspace_id, quality.run_id).to_json()
        mappings = tuple(
            replace(
                mapping,
                relationships=(
                    replace(
                        mapping.relationships[0],
                        source_column_keys=(columns["lines"]["ReviewedComponent"],),
                    ),
                ),
            )
            if mapping.target_model == "mrp.bom.line"
            else mapping
            for mapping in revision.definition.datasets
        )
        self.submit(workspace_id, mappings)
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

    def test_two_causes_keep_both_findings_without_multiplying_the_record_total(self):
        workspace_id, _revision, _columns = bom_workspace(self, two_problems=True)
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
