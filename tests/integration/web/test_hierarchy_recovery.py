"""Qualify durable hierarchy recovery across Odoo majors and connection modes."""

from unittest.mock import patch

from impodo.domain.workspace.errors import WorkspaceError
from tests.support.hierarchy_assertions import (
    assert_loaded_hierarchy,
    require_parent_receipts,
)
from tests.support.hierarchy_execution import compared_hierarchy, json2_adapters
from tests.support.hierarchy_recovery import reopen_and_check_recovery
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture

MATRIX = ((19, "LOCAL"), (19, "REMOTE"), (20, "LOCAL"), (20, "REMOTE"))


class HierarchyRecoveryTests(HierarchyWorkflowFixture):
    def test_interruption_before_target_dispatch_recovers_only_after_exact_absence(
        self,
    ):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                self._recover(major, mode, "before_dispatch")

    def test_interruption_after_target_commit_recovers_without_duplicate_parent(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                self._recover(major, mode, "after_commit")

    def test_interruption_after_durable_receipt_reopens_and_resumes_children(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                self._recover(major, mode, "after_receipt")

    def _compared(self, major, mode):
        return compared_hierarchy(
            self,
            major=major,
            mode=mode,
            source_shape="generated" if major == 19 else "supplied",
        )

    def _interrupt(self, workspace_id, target, snapshot, identities, phase):
        def crash(*_args, **_kwargs):
            raise RuntimeError("Fictional process interruption")

        writer, _ = json2_adapters(self, workspace_id, target)
        journal = self.context.execution.journal
        method = (
            "record_batch_started" if phase == "before_dispatch" else "record_outcomes"
        )
        original = getattr(journal, method)

        def record_then_interrupt(*args, **kwargs):
            original(*args, **kwargs)
            if phase != "after_commit":
                crash()

        if phase == "after_commit":
            target.after_create = crash
        with (
            patch.object(journal, method, side_effect=record_then_interrupt),
            self.assertRaisesRegex(RuntimeError, "Fictional process interruption"),
        ):
            self.context.execution.execute(
                workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=writer,
                actor=self.actor,
                **identities,
            )
        target.after_create = None
        run = journal.get_current_run(workspace_id)
        self.assertEqual(run.status.value, "RUNNING")
        self.assertTrue(
            all(model == "product.category" for model, _, _ in target.writes)
        )
        if phase == "before_dispatch":
            self.assertEqual(target.writes, [])
        else:
            self.assertGreater(len(target.writes), 0)
        if phase == "after_receipt":
            self.assertGreater(run.committed_count, 0)
        else:
            self.assertEqual(run.committed_count, 0)
        return run

    def _recover(self, major, mode, phase):
        workspace_id, _, (target, snapshot, identities) = self._compared(major, mode)
        receipts = require_parent_receipts(self, workspace_id, target)
        interrupted = self._interrupt(workspace_id, target, snapshot, identities, phase)
        before = {key: dict(value) for key, value in target.records.items()}
        write, recovery = reopen_and_check_recovery(
            self, workspace_id, target, snapshot, interrupted, identities
        )
        writer, reader = json2_adapters(self, workspace_id, target)
        plan = self.context.execution.review_recovery(
            workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            actor=self.actor,
            **write,
        )
        if phase == "before_dispatch":
            self.assertGreater(plan.safe_to_retry_count, 0)
        else:
            self.assertEqual(plan.safe_to_retry_count, 0)
        self.assertGreater(plan.resume_count, 0)
        completed = self.context.execution.resume(
            workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            executor=writer,
            actor=self.actor,
            **write,
        )
        self.assertEqual(completed.status.value, "COMPLETED")
        self.assertEqual(completed.committed_count, 13)
        self.assertTrue(
            all(target.records[key] == value for key, value in before.items())
        )
        self.assertGreater(len(receipts), 0)
        assert_loaded_hierarchy(self, target)
        result = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=completed.run_id,
            reader=reader,
            actor=self.actor,
            **write,
        )
        self.assertEqual(result.status.value, "VERIFIED", result.rows)
        self.assertEqual(result.verified_count, 18)

    def test_known_transport_rejection_stops_before_any_dependent_create(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                self._rejected(major, mode, unknown=False)

    def test_unknown_transport_outcome_never_retries_a_committed_parent(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                self._rejected(major, mode, unknown=True)

    def _rejected(self, major, mode, *, unknown):
        workspace_id, _, (target, snapshot, identities) = self._compared(major, mode)
        writer, _ = json2_adapters(self, workspace_id, target)
        transport = writer.transport
        attempts = []

        def fault(url, *args):
            if url.endswith(("/create", "/load")):
                attempts.append(url)
                if not unknown:
                    return 400, {"error": "Fictional validation rejection"}
                transport(url, *args)
                raise TimeoutError("Fictional response lost after commit")
            return transport(url, *args)

        with patch.object(writer, "transport", side_effect=fault):
            run = self.context.execution.execute(
                workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=writer,
                actor=self.actor,
                **identities,
            )
        self.assertEqual(
            run.status.value, "OUTCOME_UNKNOWN" if unknown else "COMPLETED_WITH_ERRORS"
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(target.writes), 1 if unknown else 0)
        self.assertTrue(
            all(model == "product.category" for model, _, _ in target.writes)
        )
        self.assertEqual(run.committed_count, 0)

    def test_changed_existing_parent_stops_before_journal_or_write(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                workspace_id, _, (target, snapshot, identities) = self._compared(
                    major, mode
                )
                record = target.records.pop(("product.category", 101))
                target.records[("product.category", 900)] = record
                for (model, _), values in target.records.items():
                    if model == "product.category" and values.get("parent_id") == 101:
                        values["parent_id"] = 900
                writer, _ = json2_adapters(self, workspace_id, target)
                with self.assertRaises(WorkspaceError):
                    self.context.execution.execute(
                        workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=writer,
                        actor=self.actor,
                        **identities,
                    )
                self.assertIsNone(
                    self.context.execution.journal.get_current_run(workspace_id)
                )
                self.assertEqual(target.writes, [])

    def test_stale_comparison_hash_stops_before_journal_or_write(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                workspace_id, _, (target, _, identities) = self._compared(major, mode)
                writer, _ = json2_adapters(self, workspace_id, target)
                with self.assertRaises(WorkspaceError):
                    self.context.execution.execute(
                        workspace_id,
                        expected_snapshot_hash="sha256:" + "0" * 64,
                        executor=writer,
                        actor=self.actor,
                        **identities,
                    )
                self.assertIsNone(
                    self.context.execution.journal.get_current_run(workspace_id)
                )
                self.assertEqual(target.writes, [])

    def test_missing_or_ambiguous_existing_parent_readback_never_releases_children(
        self,
    ):
        for major, mode in MATRIX:
            for fault in ("missing", "ambiguous"):
                with self.subTest(major=major, mode=mode, fault=fault):
                    workspace_id, _, (target, snapshot, identities) = self._compared(
                        major, mode
                    )
                    interrupted = self._interrupt(
                        workspace_id, target, snapshot, identities, "after_commit"
                    )
                    if fault == "missing":
                        target.records.pop(("product.category", 101))
                    else:
                        target.records[("product.category", 900)] = dict(
                            target.records[("product.category", 101)]
                        )
                    writer, reader = json2_adapters(self, workspace_id, target)
                    writes = tuple(target.writes)
                    write = {
                        key: value
                        for key, value in identities.items()
                        if key.startswith("write_")
                    }
                    recovery = self.context.reconciliation.assess_recovery(
                        workspace_id,
                        expected_execution_run_id=interrupted.run_id,
                        reader=reader,
                        actor=self.actor,
                        targeted=True,
                        **write,
                    )
                    self.assertEqual(
                        self.context.execution.journal.get_current_run(workspace_id),
                        interrupted,
                    )
                    # Targeted read-back proves the interrupted root first.
                    # Resume then rechecks every ancestor needed by its children.
                    with self.assertRaises(WorkspaceError):
                        self.context.execution.resume(
                            workspace_id,
                            expected_execution_run_id=interrupted.run_id,
                            recovery=recovery,
                            executor=writer,
                            actor=self.actor,
                            **write,
                        )
                    self.assertEqual(tuple(target.writes), writes)
                    current = self.context.execution.journal.get_current_run(workspace_id)
                    self.assertTrue(all(
                        row.status.value != "COMMITTED"
                        for row in current.rows if row.target_model == "product.template"
                    ))
