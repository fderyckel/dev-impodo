"""Generated hierarchy identities remain readable during ordinary Match data checks."""

from dataclasses import replace

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyIdentityReadTests(HierarchyWorkflowFixture):
    def test_original_table_can_read_its_generated_parent_path(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._check_original_parent_path(major, mode)

    def _check_original_parent_path(self, major, mode):
        workspace, _validation = self.hierarchy(major=major, mode=mode, check_only=True)
        plan = self.context.queries.get_derived_entity_plan(workspace)
        link = derived_dataset_links(plan)[0]
        physical = self.context.queries.get_source_selection(workspace)
        original = next(item for item in physical.datasets if item.name == "products")
        reference = next(
            column.stable_key
            for column in original.columns
            if column.source_name == "Reference"
        )
        rows = run_local_operation_sync(
            self.context.categorical_coverage.source_key_tuples,
            workspace,
            original.dataset_id,
            (reference, link.source_column_key),
        )
        self.assertEqual(
            rows,
            (
                ("PX101", "machines / pumps / service / seals"),
                ("PX102", "machines / valves / service / seals"),
                ("PX103", "maintenance / pumps / service / seals"),
                ("PX104", "machines / pumps"),
                ("PX105", "machines"),
                ("PX106", "tools"),
            ),
        )
        choices = run_local_operation_sync(
            self.context.categorical_coverage.source_value_choices,
            workspace,
            original.dataset_id,
            link.source_column_key,
        )
        self.assertEqual(len(choices), 6)

    def test_wrong_existing_record_lookup_reports_mapping_issues_without_snapshot_error(
        self,
    ):
        workspace, _validation = self.hierarchy(major=20, mode="LOCAL", check_only=True)
        revision = self.context.queries.get_mapping_revision(workspace)
        draft = self.context.queries.get_mapping_working_draft(workspace)
        mappings = tuple(
            replace(
                item,
                relationships=(
                    replace(
                        item.relationships[0],
                        resolver=RelationshipResolver(
                            ResolverOrigin.TARGET_CATALOG,
                            model="product.category",
                            key_mappings=(
                                ReferenceKeyMapping(
                                    item.relationships[0].source_column_keys[0], "name"
                                ),
                            ),
                        ),
                    ),
                ),
            )
            if item.target_model == "product.template"
            else item
            for item in revision.definition.datasets
        )
        _checked, validation = run_local_operation_sync(
            self.context.mapping_workspace.check_definition,
            workspace,
            datasets=mappings,
            expected_parent_version=revision.version,
            expected_working_draft_version=draft.version,
            actor=self.actor,
        )
        self.assertEqual(validation.status.value, "INVALID")
        self.assertIn(
            "MAPPING_BUSINESS_KEY_NOT_GOVERNED",
            {item.code for item in validation.issues},
        )

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
