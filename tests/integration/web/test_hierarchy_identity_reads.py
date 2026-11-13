"""Generated hierarchy identities remain readable during ordinary Match data checks."""

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyIdentityReadTests(HierarchyWorkflowFixture):
    def test_check_matches_reads_generated_identity_and_parent_values(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace, validation = self.hierarchy(
                        major=major, mode=mode, check_only=True
                    )
                    self.assertNotEqual(
                        validation.status.value, "INVALID", validation.issues
                    )
                    plan = self.context.queries.get_derived_entity_plan(workspace)
                    original_plan = plan.to_json()
                    link = derived_dataset_links(plan)[0]
                    with collect_duckdb_request_timings() as timings:
                        rows = run_local_operation_sync(
                            self.context.categorical_coverage.source_key_tuples,
                            workspace,
                            link.derived_dataset_id,
                            (
                                link.canonical_key_column_key,
                                link.name_column_key,
                                link.parent_key_column_key,
                            ),
                        )
                    self.assertIn(("machines / pumps", "Pumps", "machines"), rows)
                    self.assertIn(("maintenance / pumps", "Pumps", "maintenance"), rows)
                    self.assertIn(("tools", "Tools", None), rows)
                    self.assertEqual(len({row[0] for row in rows}), len(rows))
                    self.assertEqual(
                        self.context.queries.get_derived_entity_plan(
                            workspace
                        ).to_json(),
                        original_plan,
                    )
                    self.assertEqual(timings.lock_retry_count, 0)
                    # One operation retains the registry/source owners; the
                    # narrow generated-value read adds one ephemeral owner.
                    self.assertLessEqual(timings.connection_count, 6)
