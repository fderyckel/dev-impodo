from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.adapters.scenarios import (
    ScenarioExecutionJournal,
    ScenarioReconciliationResults,
)
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.reconciliation import ReconciliationRun, ReconciliationRunStatus
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError


HASH = "sha256:" + "4" * 64
WORKSPACE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _run() -> ExecutionRun:
    return ExecutionRun(
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        snapshot_hash=HASH,
        snapshot_root_hash=HASH,
        preflight_run_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        target_hash=HASH,
        target_database="impodo_scenario_contact",
        batch_rows=10,
        status=ExecutionRunStatus.RUNNING,
        started_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        started_by="Local operator",
        completed_at=None,
        rows=(
            ExecutionRowAttempt(
                row_id=HASH,
                dataset="contacts",
                source_row=1,
                target_model="res.partner",
                operation="CREATE",
                field_names=("name", "ref"),
                proposed_external_id="",
            ),
        ),
    )


class ScenarioExecutionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(__file__).resolve().parents[3] / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"scenario-evidence-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_journal_is_reloadable_and_refuses_a_second_start(self) -> None:
        journal = ScenarioExecutionJournal(self.root)
        run = _run()
        journal.start_run(WORKSPACE_ID, run, actor=LOCAL_ACTOR)

        reloaded = ScenarioExecutionJournal(self.root).get_current_run(WORKSPACE_ID)

        self.assertEqual(reloaded, run)
        with self.assertRaisesRegex(WorkspaceError, "already has execution evidence"):
            journal.start_run(WORKSPACE_ID, run, actor=LOCAL_ACTOR)

    def test_row_transition_and_finish_are_atomic_and_reloadable(self) -> None:
        journal = ScenarioExecutionJournal(self.root)
        run = _run()
        journal.start_run(WORKSPACE_ID, run, actor=LOCAL_ACTOR)
        committed = replace(
            run.rows[0],
            status=ExecutionRowStatus.COMMITTED,
            attempt=1,
            odoo_id=42,
        )

        journal.record_outcomes(WORKSPACE_ID, RUN_ID, (committed,))
        finished = journal.finish_run(
            WORKSPACE_ID,
            RUN_ID,
            ExecutionRunStatus.COMPLETED,
            actor=LOCAL_ACTOR,
        )

        self.assertIs(finished.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(finished.rows[0].odoo_id, 42)
        self.assertEqual(
            ScenarioExecutionJournal(self.root).get_run(WORKSPACE_ID, RUN_ID),
            finished,
        )
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_reconciliation_result_is_hash_checked_and_reloadable(self) -> None:
        repository = ScenarioReconciliationResults(self.root)
        report = ReconciliationRun(
            reconciliation_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            workspace_id=WORKSPACE_ID,
            execution_run_id=RUN_ID,
            snapshot_hash=HASH,
            target_hash=HASH,
            target_database="impodo_scenario_contact",
            status=ReconciliationRunStatus.VERIFIED,
            verified_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            verified_by="Local operator",
            unchanged_count=1,
            rows=(),
        )

        repository.publish(WORKSPACE_ID, report, actor=LOCAL_ACTOR)

        self.assertEqual(repository.get_current(WORKSPACE_ID, RUN_ID), report)


if __name__ == "__main__":
    unittest.main()
