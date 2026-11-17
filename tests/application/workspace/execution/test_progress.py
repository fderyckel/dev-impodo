"""Load progress preserves saved outcomes with work bounded by changed rows."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import ClassVar

from impodo.application.workspace.execution.job_models import LoadPhase
from impodo.application.workspace.execution.progress import (
    ExecutionProgressAccumulator,
    ExecutionProgressPublisher,
    execution_progress,
)
from impodo.domain.execution.load_control import LoadControl, LoadControlState
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)


def _row(index, *, status=ExecutionRowStatus.PLANNED, operation="CREATE", component=0):
    return ExecutionRowAttempt(
        row_id=f"row-{index}",
        dataset="records",
        source_row=index + 1,
        target_model="x.record",
        operation=operation,
        field_names=("name",),
        proposed_external_id="",
        status=status,
        schedule_component=component,
    )


def _run(rows):
    return ExecutionRun(
        run_id="run",
        workspace_id="workspace",
        snapshot_hash="snapshot",
        snapshot_root_hash="root",
        preflight_run_id="preflight",
        target_hash="target",
        target_database="database",
        batch_rows=25,
        status=ExecutionRunStatus.RUNNING,
        started_at=datetime.now(UTC),
        started_by="Operator",
        completed_at=None,
        rows=rows,
    )


class _ObservedAttempt(ExecutionRowAttempt):
    status_reads: ClassVar[int] = 0

    def __getattribute__(self, name):
        if name == "status":
            type(self).status_reads += 1
        return super().__getattribute__(name)


class _ObservedRows(tuple):
    visits = 0
    iteration_allowed = True

    def __iter__(self):
        if not self.iteration_allowed:
            raise AssertionError("progress rescanned the durable journal")
        for row in super().__iter__():
            self.visits += 1
            yield row


class ExecutionProgressTests(unittest.TestCase):
    def test_resume_initializes_all_saved_outcomes_and_active_group(self):
        run = _run(
            (
                _row(0, status=ExecutionRowStatus.COMMITTED, component=0),
                _row(
                    1,
                    status=ExecutionRowStatus.COMMITTED,
                    operation="UPDATE",
                    component=0,
                ),
                _row(2, status=ExecutionRowStatus.PARTIALLY_APPLIED, component=1),
                _row(3, status=ExecutionRowStatus.IN_FLIGHT, component=2),
                _row(4, status=ExecutionRowStatus.RETRY_READY, component=2),
                _row(5, status=ExecutionRowStatus.FAILED, component=3),
                _row(6, status=ExecutionRowStatus.OUTCOME_UNKNOWN, component=3),
                _row(7, status=ExecutionRowStatus.BLOCKED, component=3),
            )
        )
        progress = execution_progress(run)
        self.assertEqual((progress.total_count, progress.completed_rows), (8, 5))
        self.assertEqual((progress.created_count, progress.updated_count), (2, 1))
        self.assertEqual(progress.attention_count, 3)
        self.assertEqual(progress.partially_applied_count, 1)
        self.assertEqual((progress.in_flight_count, progress.retry_ready_count), (1, 1))
        self.assertEqual((progress.rejected_count, progress.blocked_count), (1, 1))
        self.assertEqual(
            (progress.load_group_number, progress.load_group_count), (3, 4)
        )
        self.assertFalse(hasattr(progress, "rows"))
        with self.assertRaises(FrozenInstanceError):
            progress.total_count = 100

    def test_relationship_completion_does_not_count_a_second_create(self):
        planned = _row(0)
        accumulator = ExecutionProgressAccumulator(_run((planned,)))
        in_flight = replace(planned, status=ExecutionRowStatus.IN_FLIGHT)
        accumulator.transition(planned, in_flight)
        self.assertEqual(accumulator.snapshot().completed_rows, 0)
        partial = replace(
            in_flight, status=ExecutionRowStatus.PARTIALLY_APPLIED, odoo_id=11
        )
        accumulator.transition(in_flight, partial)
        progress = accumulator.snapshot()
        self.assertEqual(progress.phase, LoadPhase.RELATIONSHIPS)
        self.assertEqual((progress.created_count, progress.completed_rows), (1, 0))
        completed = replace(partial, status=ExecutionRowStatus.COMMITTED)
        accumulator.transition(partial, completed)
        progress = accumulator.snapshot()
        self.assertEqual(progress.phase, LoadPhase.RELATIONSHIPS)
        self.assertEqual((progress.created_count, progress.completed_rows), (1, 1))
        self.assertEqual(progress.relationship_total_count, 1)
        self.assertEqual(progress.partially_applied_count, 0)

    def test_rejected_relationship_and_terminal_attention_preserve_create_receipt(self):
        partial = _row(0, status=ExecutionRowStatus.PARTIALLY_APPLIED)
        run = _run((partial,))
        accumulator = ExecutionProgressAccumulator(run)
        rejected = replace(
            partial,
            diagnostic=OdooWriteDiagnostic(
                code=WriteFailureCode.REJECTED,
                model="x.record",
                method="write",
                batch_size=1,
            ),
        )
        accumulator.transition(partial, rejected)
        self.assertEqual(accumulator.snapshot().rejected_count, 1)
        accumulator.finish(
            replace(
                run,
                status=ExecutionRunStatus.COMPLETED_WITH_ERRORS,
                load_control=LoadControl(
                    state=LoadControlState.PAUSED, reason="CONSECUTIVE_REJECTIONS"
                ),
            )
        )
        progress = accumulator.snapshot()
        self.assertEqual((progress.created_count, progress.attention_count), (1, 1))
        self.assertEqual(
            (progress.load_control_state, progress.load_pause_reason),
            ("PAUSED", "CONSECUTIVE_REJECTIONS"),
        )

    def test_throttling_keeps_boundaries_and_terminal_state_immediate(self):
        rows = tuple(_row(index) for index in range(3))
        run = _run(rows)
        publications = []
        publisher = ExecutionProgressPublisher(run, publications.append, row_interval=2)
        publisher.publish(force=True)
        publisher.transition(
            rows[0], replace(rows[0], status=ExecutionRowStatus.COMMITTED)
        )
        publisher.publish(changed_rows=1)
        self.assertEqual(len(publications), 1)
        publisher.transition(
            rows[1], replace(rows[1], status=ExecutionRowStatus.FAILED)
        )
        publisher.publish(changed_rows=1)
        self.assertEqual(len(publications), 2)
        publisher.transition(
            rows[2], replace(rows[2], status=ExecutionRowStatus.OUTCOME_UNKNOWN)
        )
        publisher.finish(replace(run, status=ExecutionRunStatus.OUTCOME_UNKNOWN))
        self.assertEqual(len(publications), 3)
        self.assertEqual(publications[-1].status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(publications[-1].attention_count, 2)

    def test_initialization_and_changed_row_work_remain_linear_as_journal_grows(self):
        for size in (100, 1_000, 10_000):
            with self.subTest(size=size):
                rows = _ObservedRows(
                    _ObservedAttempt(
                        row_id=f"row-{index}",
                        dataset="records",
                        source_row=index + 1,
                        target_model="x.record",
                        operation="CREATE",
                        field_names=("name",),
                        proposed_external_id="",
                        schedule_component=index // 25,
                    )
                    for index in range(size)
                )
                run = _run(rows)
                _ObservedAttempt.status_reads = 0
                publications = []
                publisher = ExecutionProgressPublisher(
                    run, publications.append, row_interval=25
                )
                self.assertEqual(rows.visits, size)
                rows.iteration_allowed = False
                publisher.publish(force=True)
                for index in range(size):
                    previous = rows[index]
                    current = replace(previous, status=ExecutionRowStatus.COMMITTED)
                    publisher.transition(previous, current)
                    publisher.publish(changed_rows=1)
                publisher.finish(replace(run, status=ExecutionRunStatus.COMPLETED))
                self.assertEqual(rows.visits, size)
                # The instrumented status read covers initialization, each
                # dataclass replacement, and both sides of each transition.
                self.assertLessEqual(_ObservedAttempt.status_reads, 4 * size)
                self.assertEqual(publications[-1].created_count, size)
                self.assertEqual(publications[-1].completed_rows, size)
                self.assertEqual(publications[-1].load_group_number, size // 25)
                self.assertEqual(len(publications), 2 + size // 25)

    def test_external_stop_reconciles_once_and_preserves_relationship_history(self):
        partial = _row(0, status=ExecutionRowStatus.PARTIALLY_APPLIED, component=0)
        run = _run((partial,))
        publications = []
        publisher = ExecutionProgressPublisher(
            run, publications.append, row_interval=25
        )
        publisher.publish(force=True)
        completed = replace(partial, status=ExecutionRowStatus.COMMITTED)
        publisher.transition(partial, completed)
        saved_rows = _ObservedRows((completed,))
        stopped = replace(
            run,
            rows=saved_rows,
            status=ExecutionRunStatus.COMPLETED_WITH_ERRORS,
            load_control=LoadControl(
                state=LoadControlState.STOPPED, reason="USER_STOP"
            ),
        )
        publisher.reconcile_stopped_run(stopped)
        self.assertEqual(saved_rows.visits, 1)
        saved_rows.iteration_allowed = False
        publisher.reconcile_stopped_run(stopped)
        progress = publications[-1]
        self.assertEqual(progress.phase, LoadPhase.RELATIONSHIPS)
        self.assertEqual(progress.relationship_total_count, 1)
        self.assertEqual(progress.created_count, 1)
        self.assertEqual(progress.committed_count, 1)
        self.assertEqual(progress.load_group_count, 1)
        self.assertEqual(progress.load_control_state, "STOPPED")
        self.assertEqual(saved_rows.visits, 1)

    def test_external_stop_reconcile_rejects_an_ordinary_running_notification(self):
        run = _run((_row(0),))
        publisher = ExecutionProgressPublisher(run, None, row_interval=25)
        with self.assertRaisesRegex(ValueError, "same stopped run"):
            publisher.reconcile_stopped_run(run)


if __name__ == "__main__":
    unittest.main()
