"""Keep invalid, excluded, and unsafe physical parents distinct from null roots."""

from dataclasses import replace
from uuid import uuid4

from impodo.domain.mapping.contracts import (
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from tests.support.hierarchy_cases import CATEGORY_ROWS
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyFailureWorkflowTests(HierarchyWorkflowFixture):
    def _issues(self, workspace_id):
        return self.context.quality.current_run(workspace_id)

    def _assert_populated_parent_is_not_root(self, staging):
        children = [
            row
            for row in staging.rows
            if row.target_model == "product.category"
            and row.source_identity in {("C110",), ("C120",)}
        ]
        self.assertEqual(len(children), 2)
        for row in children:
            self.assertNotEqual(row.target_scope, (None,))

    def test_duplicate_source_root_sets_aside_its_group_without_choosing_one(self):
        workspace_id, _ = self.hierarchy(
            source_shape="supplied", categories=(*CATEGORY_ROWS, CATEGORY_ROWS[0])
        )
        staging, quality = self.prepare(workspace_id)
        self.assertGreater(quality.quarantined_count, 0)
        issues = self._issues(workspace_id)
        self.assertIn(
            "INCOMING_RELATIONSHIP_AMBIGUOUS",
            {issue.reason_code for issue in issues.issues},
        )
        self._assert_populated_parent_is_not_root(staging)
        self.assertIsNone(self.context.execution.journal.get_current_run(workspace_id))

    def test_missing_source_parent_keeps_missing_reason_and_populated_scope(self):
        workspace_id, _ = self.hierarchy(
            source_shape="supplied",
            categories=tuple(row for row in CATEGORY_ROWS if row[0] != "C100"),
        )
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.excluded_count, 0)
        self.assertIn(
            "INCOMING_RELATIONSHIP_MISSING",
            {issue.reason_code for issue in self._issues(workspace_id).issues},
        )
        self._assert_populated_parent_is_not_root(staging)

    def test_quarantined_parent_remains_a_saved_parent_consequence(self):
        workspace_id, _ = self.hierarchy(
            source_shape="supplied",
            categories=tuple(
                (key, "" if key == "C100" else name, parent)
                for key, name, parent in CATEGORY_ROWS
            ),
        )
        staging, quality = self.prepare(workspace_id)
        self.assertGreater(quality.quarantined_count, 0)
        self.assertEqual(quality.excluded_count, 0)
        self.assertIn(
            "INCOMING_RELATIONSHIP_PARENT_SET_ASIDE",
            {issue.reason_code for issue in self._issues(workspace_id).issues},
        )
        self._assert_populated_parent_is_not_root(staging)

    def test_reviewed_parent_exclusion_never_promotes_its_children_to_roots(self):
        workspace_id, revision = self.hierarchy(source_shape="supplied")
        selection = self.context.queries.get_source_selection(workspace_id)
        category = next(
            item for item in selection.datasets if item.name == "categories"
        )
        key = next(
            item.stable_key for item in category.columns if item.source_name == "Key"
        )
        self.submit(
            workspace_id,
            tuple(
                replace(
                    mapping,
                    row_inclusion=RowInclusionPolicy(
                        mode=RowInclusionMode.MATCHING_ROWS,
                        conditions=(
                            RowInclusionCondition(
                                str(uuid4()),
                                key,
                                SelectionConditionOperator.NOT_EQUALS,
                                "C100",
                            ),
                        ),
                    ),
                )
                if mapping.dataset_id == category.dataset_id
                else mapping
                for mapping in revision.definition.datasets
            ),
            confirm_rows=True,
        )
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.excluded_count, 1)
        excluded = [row for row in staging.rows if row.disposition.value == "EXCLUDED"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0].lineage.source_row, 2)
        self.assertGreater(quality.quarantined_count, 0)
        self._assert_populated_parent_is_not_root(staging)

    def test_ambiguous_destination_root_blocks_the_accepted_file_journey(self):
        workspace_id, _ = self.hierarchy(source_shape="supplied")
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        target = HierarchyOdoo(schema)
        target.records[("product.category", 900)] = {
            "name": "Machines",
            "parent_id": False,
        }
        report = self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        self.assertNotEqual(report.status, "READY")
        self.assertGreater(report.ambiguous_count, 0)
        self.assertEqual(target.writes, [])
        self.assertIsNone(self.context.execution.journal.get_current_run(workspace_id))
