"""Qualify both accepted-file hierarchy shapes through persisted workflow services."""

from tests.support.hierarchy_assertions import (
    assert_frozen_hierarchy,
    assert_loaded_hierarchy,
    assert_prepared_hierarchy,
    require_parent_receipts,
)
from tests.support.hierarchy_execution import compare_hierarchy, json2_adapters
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyWorkflowTests(HierarchyWorkflowFixture):
    def test_generated_odoo19_local(self):
        self._complete(19, "LOCAL", "generated")

    def test_generated_odoo20_local(self):
        self._complete(20, "LOCAL", "generated")

    def test_generated_odoo19_remote(self):
        self._complete(19, "REMOTE", "generated")

    def test_generated_odoo20_remote(self):
        self._complete(20, "REMOTE", "generated")

    def test_supplied_odoo19_local(self):
        self._complete(19, "LOCAL", "supplied")

    def test_supplied_odoo20_local(self):
        self._complete(20, "LOCAL", "supplied")

    def test_supplied_odoo19_remote(self):
        self._complete(19, "REMOTE", "supplied")

    def test_supplied_odoo20_remote(self):
        self._complete(20, "REMOTE", "supplied")

    def _complete(self, major, mode, source_shape):
        workspace_id, _ = self.hierarchy(
            major=major, mode=mode, source_shape=source_shape
        )
        staging, quality = self.prepare(workspace_id)
        assert_prepared_hierarchy(self, workspace_id, staging, quality)
        repeated, repeated_quality = self.prepare(workspace_id)
        self.assertEqual(repeated.content_hash, staging.content_hash)
        self.assertEqual(repeated_quality.content_hash, quality.content_hash)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target, snapshot, identities = compare_hierarchy(self, workspace_id)
        assert_frozen_hierarchy(self, snapshot)
        receipts = require_parent_receipts(self, workspace_id, target)
        writer, reader = json2_adapters(self, workspace_id, target)
        execution = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=self.actor,
            **identities,
        )
        self.assertEqual(execution.status.value, "COMPLETED")
        self.assertEqual(execution.committed_count, 13)
        self.assertGreater(len(receipts), 0)
        assert_loaded_hierarchy(self, target)
        reconciliation = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=execution.run_id,
            reader=reader,
            actor=self.actor,
            **{
                key: value
                for key, value in identities.items()
                if key.startswith("write_")
            },
        )
        self.assertEqual(reconciliation.status.value, "VERIFIED", reconciliation.rows)
        self.assertEqual(reconciliation.verified_count, 18)
