"""Validate supplied execution facts before application or transport work."""

import unittest
from decimal import Decimal
from types import SimpleNamespace

from impodo.domain.execution.validation import (
    ExecutionSnapshotIssueCode,
    execution_snapshot_issue,
)
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    FieldIntent,
    plan_execution_rows,
)
from impodo.domain.shared.models import BusinessReference
from impodo.domain.workspace.workbench import OdooConnectionMode


def snapshot(major, *, operation="CREATE", fields=None, digits=()):
    dataset = ExecutionDataset(
        dataset="items",
        target_model="product.product",
        sequence=0,
        dependencies=(),
        existing_policy="update",
        identity_fields=("default_code",),
        scope_fields=(),
        field_digits=digits,
    )
    row = ExecutionRow(
        row_id="item-1",
        dataset=dataset.dataset,
        source_row=1,
        source_trace_id="source-1",
        source_identity=("ITEM-1",),
        target_model=dataset.target_model,
        business_identity=("ITEM-1",),
        business_scope=(),
        disposition=operation,
        target_match_count=0 if operation == "CREATE" else 1,
        proposed_external_id="impodo.item_1" if operation == "CREATE" else "",
        fields=fields or (FieldIntent("name", "SET_VALUE", "Item"),),
    )
    rows, plan = plan_execution_rows((row,), (dataset,))
    return SimpleNamespace(
        target_odoo_version=f"{major}.0",
        datasets=(dataset,),
        rows=rows,
        relationship_plan=plan,
    )


class ExecutionSnapshotValidationTests(unittest.TestCase):
    def test_supported_versions_and_access_modes_accept_the_same_shape(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.assertIsNone(
                        execution_snapshot_issue(snapshot(major), connection_mode=mode)
                    )

    def test_target_and_version_rejections_precede_snapshot_shape_inspection(self):
        supplied = SimpleNamespace(target_odoo_version="21.0")
        self.assertEqual(
            execution_snapshot_issue(supplied, connection_mode=None).code,
            ExecutionSnapshotIssueCode.TARGET_NOT_CONFIGURED,
        )
        self.assertEqual(
            execution_snapshot_issue(
                supplied, connection_mode=OdooConnectionMode.REMOTE
            ).code,
            ExecutionSnapshotIssueCode.UNSUPPORTED_VERSION,
        )

    def test_schedule_rejection_precedes_precision_and_relationship_checks(self):
        supplied = snapshot(
            20,
            fields=(FieldIntent("cost", "SET_VALUE", Decimal("0.0007")),),
            digits=(("cost", (12, 2)),),
        )
        supplied.relationship_plan = SimpleNamespace(
            contract_version=0,
            root_hash="",
            components=supplied.relationship_plan.components,
            blockers=(),
            completions=(),
        )
        self.assertEqual(
            execution_snapshot_issue(
                supplied, connection_mode=OdooConnectionMode.LOCAL
            ).code,
            ExecutionSnapshotIssueCode.SCHEDULE_CHANGED,
        )

    def test_precision_issue_retains_aggregate_facts_for_the_message_owner(self):
        for major in (19, 20):
            with self.subTest(major=major):
                supplied = snapshot(
                    major,
                    fields=(FieldIntent("cost", "SET_VALUE", Decimal("0.0007")),),
                    digits=(("cost", (12, 2)),),
                )
                original_rows = supplied.rows
                issue = execution_snapshot_issue(
                    supplied, connection_mode=OdooConnectionMode.REMOTE
                )
                self.assertEqual(
                    issue.code, ExecutionSnapshotIssueCode.NUMERIC_PRECISION_LOSS
                )
                self.assertEqual(issue.precision.model, "product.product")
                self.assertEqual(issue.precision.field_name, "cost")
                self.assertEqual(issue.precision.count, 1)
                self.assertEqual(issue.precision.required_places, 4)
                self.assertEqual(issue.precision.zeroed_positive_count, 1)
                self.assertIs(supplied.rows, original_rows)

    def test_remote_update_classification_preserves_the_exact_operation_boundary(self):
        fields = (
            FieldIntent(
                "category_ids",
                "SET_VALUE",
                (BusinessReference("product.category", ("Sale",)),),
                kind="relation",
                relation_operation="add",
                related_model="product.category",
            ),
        )
        for major in (19, 20):
            with self.subTest(major=major):
                supplied = snapshot(major, operation="UPDATE", fields=fields)
                issue = execution_snapshot_issue(
                    supplied, connection_mode=OdooConnectionMode.REMOTE
                )
                self.assertEqual(
                    issue.code, ExecutionSnapshotIssueCode.REMOTE_UPDATE_OPERATION
                )
                self.assertEqual((issue.dataset, issue.field), ("items", "category_ids"))
                self.assertIsNone(
                    execution_snapshot_issue(
                        supplied, connection_mode=OdooConnectionMode.LOCAL
                    )
                )

    def test_missing_dataset_is_classified_without_modifying_the_frozen_rows(self):
        supplied = snapshot(19)
        supplied.datasets = ()
        issue = execution_snapshot_issue(
            supplied, connection_mode=OdooConnectionMode.LOCAL
        )
        self.assertEqual(issue.code, ExecutionSnapshotIssueCode.MISSING_DATASET)
        self.assertEqual(issue.dataset, "items")
        self.assertEqual(supplied.rows[0].fields[0].value, "Item")

    def test_booleans_are_not_reported_as_numeric_precision_loss(self):
        supplied = snapshot(
            20,
            fields=(FieldIntent("active", "SET_VALUE", True),),
            digits=(("active", (1, 0)),),
        )
        self.assertIsNone(
            execution_snapshot_issue(supplied, connection_mode=OdooConnectionMode.LOCAL)
        )


if __name__ == "__main__":
    unittest.main()
