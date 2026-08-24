from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
import time
import unittest

from impodo.application.load_job_service import (
    LoadJobManager,
    LoadJobResult,
)
from impodo.domain.execution import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)


def _run(*, terminal: bool) -> ExecutionRun:
    now = datetime.now(timezone.utc)
    rows = (
        ExecutionRowAttempt(
            row_id="create-ready",
            dataset="contacts",
            source_row=1,
            target_model="res.partner",
            operation="CREATE",
            field_names=("name",),
            proposed_external_id="impodo.contact_1",
            status=ExecutionRowStatus.COMMITTED,
            attempt=1,
            odoo_id=101,
        ),
        ExecutionRowAttempt(
            row_id="update-ready",
            dataset="contacts",
            source_row=2,
            target_model="res.partner",
            operation="UPDATE",
            field_names=("name",),
            proposed_external_id="",
            status=ExecutionRowStatus.COMMITTED,
            attempt=1,
            odoo_id=42,
        ),
        ExecutionRowAttempt(
            row_id="create-relationship",
            dataset="contacts",
            source_row=3,
            target_model="res.partner",
            operation="CREATE",
            field_names=("name", "parent_id"),
            proposed_external_id="impodo.contact_3",
            status=ExecutionRowStatus.PARTIALLY_APPLIED,
            attempt=1,
            odoo_id=103,
        ),
    )
    return ExecutionRun(
        run_id="11111111-1111-4111-8111-111111111111",
        project_id="workspace-1",
        snapshot_hash="snapshot",
        snapshot_root_hash="root",
        preflight_run_id="22222222-2222-4222-8222-222222222222",
        target_hash="target",
        target_database="migration",
        batch_rows=10,
        status=(
            ExecutionRunStatus.COMPLETED_WITH_ERRORS
            if terminal
            else ExecutionRunStatus.RUNNING
        ),
        started_at=now,
        started_by="Data manager",
        completed_at=now if terminal else None,
        rows=rows,
    )


def _wait_for_terminal(manager: LoadJobManager, job_id: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = manager.get("workspace-1", job_id)
        if job.terminal:
            return job
        time.sleep(0.005)
    raise AssertionError("load job did not finish")


class LoadJobManagerTests(unittest.TestCase):
    def test_reports_only_saved_execution_outcomes_and_verification_state(self) -> None:
        manager = LoadJobManager()

        def work(report_writing, report_verifying):
            report_writing(_run(terminal=False))
            report_verifying(_run(terminal=True))
            return LoadJobResult(
                execution_run_id="11111111-1111-4111-8111-111111111111",
                verification_complete=True,
            )

        queued = manager.enqueue(
            "workspace-1",
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Production",
            total_rows=3,
            work=work,
        )
        finished = _wait_for_terminal(manager, queued.job_id)
        manager.shutdown()

        self.assertEqual(finished.status.value, "SUCCEEDED")
        self.assertEqual(finished.completed_rows, 3)
        self.assertEqual(finished.created_count, 2)
        self.assertEqual(finished.updated_count, 1)
        self.assertEqual(finished.relationship_pending_count, 1)
        self.assertEqual(finished.attention_count, 1)
        self.assertEqual(finished.progress_percent, 100)
        self.assertTrue(finished.verification_complete)

    def test_duplicate_submission_reuses_the_active_job(self) -> None:
        manager = LoadJobManager()
        started = Event()
        release = Event()

        def work(_report_writing, _report_verifying):
            started.set()
            release.wait(1)
            return LoadJobResult(execution_run_id="run-1", verification_complete=False)

        first = manager.enqueue(
            "workspace-1",
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            work=work,
        )
        self.assertTrue(started.wait(1))
        duplicate = manager.enqueue(
            "workspace-1",
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            work=work,
        )
        release.set()
        _wait_for_terminal(manager, first.job_id)
        manager.shutdown()

        self.assertEqual(duplicate.job_id, first.job_id)

    def test_unexpected_failure_does_not_expose_internal_detail(self) -> None:
        manager = LoadJobManager()

        def work(_report_writing, _report_verifying):
            raise RuntimeError("secret implementation detail")

        queued = manager.enqueue(
            "workspace-1",
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            work=work,
        )
        failed = _wait_for_terminal(manager, queued.job_id)
        manager.shutdown()

        self.assertEqual(failed.status.value, "FAILED")
        self.assertNotIn("secret implementation detail", failed.failure_message)
        self.assertIn("stopped the load", failed.failure_message)


if __name__ == "__main__":
    unittest.main()
