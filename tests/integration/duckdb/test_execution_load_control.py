"""Real journal transactions preserve receipts across stop and split boundaries."""

from dataclasses import replace
import unittest

from impodo.adapters.duckdb.execution_repository import ExecutionRepository
from impodo.domain.execution.load_control import (
    LoadControl,
    LoadControlState,
    LoadHalted,
    RejectionPolicy,
)
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.integration.duckdb import test_execution_repository as fixtures


class ExecutionLoadControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionRepositoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repository = self.fixture.repository
        self.workspace_id = self.fixture.workspace_state.workspace_id

    def test_stop_during_request_retains_receipt_and_blocks_next_call(self):
        run = self.fixture._run()
        self.repository.start_run(
            self.workspace_id,
            run,
            actor=LOCAL_ACTOR,
            load_control=LoadControl(policy=RejectionPolicy.CONTINUE),
        )
        started = self.fixture._start_batch(
            run, (run.rows[0],), phase="CREATE", batch=0
        )[0]
        self.repository.request_load_stop(
            self.workspace_id, run.run_id, actor=LOCAL_ACTOR
        )
        committed = replace(started, status=ExecutionRowStatus.COMMITTED, odoo_id=123)
        self.repository.record_outcomes(self.workspace_id, run.run_id, (committed,))
        with self.assertRaises(LoadHalted):
            self.fixture._start_batch(run, (run.rows[1],), phase="UPDATE", batch=1)
        saved = ExecutionRepository(self.fixture.database).get_run(
            self.workspace_id, run.run_id
        )
        self.assertEqual(saved.rows[0], committed)
        self.assertEqual(saved.rows[1], run.rows[1])
        self.assertEqual(saved.load_control.state, LoadControlState.STOP_REQUESTED)
        path = (
            self.repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(path) as connection:
            events = connection.execute(
                "SELECT control_json FROM execution_load_control_event WHERE run_id = ? ORDER BY ordinal",
                [run.run_id],
            ).fetchall()
        self.assertEqual(
            [LoadControl.from_json(row[0]).state for row in events],
            [LoadControlState.RUNNING, LoadControlState.STOP_REQUESTED],
        )

    def test_split_retains_original_receipt_and_counts_only_final_rejections(self):
        run = self.fixture._run()
        run = replace(
            run, rows=tuple(replace(row, operation="CREATE") for row in run.rows)
        )
        self.repository.start_run(
            self.workspace_id,
            run,
            actor=LOCAL_ACTOR,
            load_control=LoadControl(policy=RejectionPolicy.CONTINUE),
        )
        started = self.fixture._start_batch(run, run.rows, phase="CREATE", batch=0)
        error = OdooWriteRejected(
            "safe",
            diagnostic=OdooWriteDiagnostic(
                WriteFailureCode.VALIDATION, "res.partner", "load", batch_size=2
            ),
        )
        retries = self.repository.record_rejected_batch(
            self.workspace_id, run.run_id, started, error
        )
        self.assertEqual(
            self.repository.get_load_control(self.workspace_id, run.run_id).windows, ()
        )
        child = self.fixture._start_batch(run, (retries[0],), phase="CREATE", batch=1)[
            0
        ]
        self.assertEqual(child.proposed_external_id, started[0].proposed_external_id)
        self.assertEqual(child.attempt, 2)
        failed = replace(
            child, status=ExecutionRowStatus.FAILED, diagnostic=error.diagnostic
        )
        self.repository.record_outcomes(self.workspace_id, run.run_id, (failed,))
        saved = self.repository.get_load_control(self.workspace_id, run.run_id)
        self.assertEqual(saved.windows[0].recent, (True,))
        self.assertEqual(saved.isolation_calls, 1)
        path = (
            self.repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(path) as connection:
            evidence = connection.execute(
                "SELECT rejection_json FROM execution_rejected_transport"
            ).fetchall()
        self.assertEqual(len(evidence), 1)
        self.assertIn(started[1].row_id, evidence[0][0])

    def test_legacy_journal_is_strict_and_preserves_row_json(self):
        run = self.fixture._run()
        self.repository.start_run(self.workspace_id, run, actor=LOCAL_ACTOR)
        saved = self.repository.get_run(self.workspace_id, run.run_id)
        self.assertIsNone(saved.load_control)
        self.assertEqual(
            tuple(row.to_json() for row in saved.rows),
            tuple(row.to_json() for row in run.rows),
        )

    def test_repeated_stop_after_completion_is_idempotent(self):
        run = self.fixture._run()
        self.repository.start_run(
            self.workspace_id, run, actor=LOCAL_ACTOR, load_control=LoadControl()
        )
        self.repository.request_load_stop(
            self.workspace_id, run.run_id, actor=LOCAL_ACTOR
        )
        self.repository.record_outcomes(
            self.workspace_id,
            run.run_id,
            tuple(replace(row, status=ExecutionRowStatus.BLOCKED) for row in run.rows),
        )
        stopped = self.repository.finish_run(
            self.workspace_id,
            run.run_id,
            ExecutionRunStatus.COMPLETED_WITH_ERRORS,
            actor=LOCAL_ACTOR,
        )
        repeated = self.repository.request_load_stop(
            self.workspace_id, run.run_id, actor=LOCAL_ACTOR
        )
        self.assertEqual(repeated, stopped.load_control)
        self.assertEqual(repeated.state, LoadControlState.STOPPED)
