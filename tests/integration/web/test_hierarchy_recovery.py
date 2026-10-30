"""Keep real parent receipts durable across interruption and exact read-back."""

from unittest.mock import patch

from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.hierarchy_execution import compared_hierarchy
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyRecoveryTests(HierarchyWorkflowFixture):
    def test_interruption_after_odoo_commit_recovers_without_duplicate_parent(self):
        self._recover("LOCAL", after_receipt=False)

    def test_remote_interruption_after_odoo_commit_recovers_external_id(self):
        self._recover("REMOTE", after_receipt=False)

    def test_interruption_after_receipt_retains_parent_and_resumes_children(self):
        self._recover("LOCAL", after_receipt=True)

    def _recover(self, mode, *, after_receipt):
        workspace_id, _revision, (target, snapshot, identities) = compared_hierarchy(
            self, mode=mode
        )

        def crash(*args):
            raise RuntimeError("Fictional process interruption")

        if not after_receipt:
            target.after_create = crash
        journal = self.context.execution.journal
        record_outcomes = journal.record_outcomes

        def record_then_interrupt(*args, **kwargs):
            record_outcomes(*args, **kwargs)
            if after_receipt:
                crash()

        with (
            patch.object(journal, "record_outcomes", side_effect=record_then_interrupt),
            self.assertRaisesRegex(RuntimeError, "Fictional process interruption"),
        ):
            self.context.execution.execute(
                workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=target,
                actor=self.actor,
                **identities,
            )
        interrupted = self.context.execution.journal.get_current_run(workspace_id)
        self.assertEqual(interrupted.status.value, "RUNNING")
        if not after_receipt:
            self.assertTrue(
                all(model == "product.category" for model, _, _ in target.writes)
            )
        before = dict(target.records)
        target.after_create = None
        write = {
            key: value for key, value in identities.items() if key.startswith("write_")
        }
        recovery = self.context.reconciliation.assess_recovery(
            workspace_id,
            expected_execution_run_id=interrupted.run_id,
            reader=target,
            actor=self.actor,
            targeted=True,
            **write,
        )
        plan = self.context.execution.review_recovery(
            workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            actor=self.actor,
            **write,
        )
        self.assertEqual(plan.safe_to_retry_count, 0)
        self.assertGreater(plan.resume_count, 0)
        completed = self.context.execution.resume(
            workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            executor=target,
            actor=self.actor,
            **write,
        )
        self.assertEqual(completed.status.value, "COMPLETED")
        self.assertEqual(completed.committed_count, 13)
        self.assertTrue(
            all(target.records[key] == value for key, value in before.items())
        )
        self.assertEqual(
            sum(len(identifiers) for _, _, identifiers in target.writes), 13
        )
        result = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=completed.run_id,
            reader=target,
            actor=self.actor,
            **write,
        )
        self.assertEqual(result.status.value, "VERIFIED", result.rows)

    def test_known_rejection_stops_before_any_dependent_create(self):
        self._rejected(
            OdooWriteRejected("Fictional validation rejection"), "COMPLETED_WITH_ERRORS"
        )

    def test_unknown_outcome_stops_without_automatic_retry(self):
        self._rejected(
            OdooWriteOutcomeUnknown("Fictional lost response"), "OUTCOME_UNKNOWN"
        )

    def _rejected(self, error, expected):
        workspace_id, _revision, (target, snapshot, identities) = compared_hierarchy(
            self
        )
        attempts = []

        def reject():
            attempts.append(1)
            raise error

        target.before_create = reject
        run = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=target,
            actor=self.actor,
            **identities,
        )
        self.assertEqual(run.status.value, expected)
        self.assertEqual(attempts, [1])
        self.assertEqual(target.writes, [])

    def test_retargeted_existing_parent_stops_before_journal_or_write(self):
        workspace_id, _revision, (target, snapshot, identities) = compared_hierarchy(
            self
        )
        record = target.records.pop(("product.category", 101))
        target.records[("product.category", 900)] = record
        for (model, _), values in target.records.items():
            if model == "product.category" and values.get("parent_id") == 101:
                values["parent_id"] = 900
        with self.assertRaises(WorkspaceError):
            self.context.execution.execute(
                workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=target,
                actor=self.actor,
                **identities,
            )
        self.assertIsNone(self.context.execution.journal.get_current_run(workspace_id))
        self.assertEqual(target.writes, [])
