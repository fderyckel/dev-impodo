from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
import time
import unittest

from impodo.application.workspace.execution.load_jobs import (
    LoadJobManager,
    LoadJobResult,
)
from impodo.domain.execution import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.migration_foundation import MigrationIdentifierConfusionError
from impodo.load_jobs import LoadJobStatus
from impodo.workspace_access import WorkspaceAccessContext


PROJECT_ID = "10000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
DATA_VERSION_ID = "30000000-0000-4000-8000-000000000001"
MIGRATION_RUN_ID = "40000000-0000-4000-8000-000000000001"


def _access_context() -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        project_id=PROJECT_ID,
        workspace_id=WORKSPACE_ID,
        data_version_id=DATA_VERSION_ID,
        migration_run_id=MIGRATION_RUN_ID,
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
        workspace_id=WORKSPACE_ID,
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
        job = manager.get(WORKSPACE_ID, job_id)
        if job.terminal:
            return job
        time.sleep(0.005)
    raise AssertionError("load job did not finish")


class LoadJobManagerTests(unittest.TestCase):
    def test_rejects_mismatched_access_context_before_queueing(self) -> None:
        manager = LoadJobManager()

        with self.assertRaises(MigrationIdentifierConfusionError):
            manager.enqueue(
                str("50000000-0000-4000-8000-000000000001"),
                "Wrong workspace",
                target_database="migration",
                target_server="odoo.example.test",
                target_environment="Production",
                total_rows=1,
                access_context=_access_context(),
                work=lambda _access, _writing, _verifying: LoadJobResult(
                    execution_run_id="never",
                    verification_complete=False,
                ),
            )

        self.assertIsNone(manager.latest(WORKSPACE_ID))
        manager.shutdown()

    def test_reports_only_saved_execution_outcomes_and_verification_state(self) -> None:
        published = []
        manager = LoadJobManager(status_listener=published.append)

        def work(access_context, report_writing, report_verifying):
            self.assertEqual(access_context, _access_context())
            report_writing(_run(terminal=False))
            report_verifying(_run(terminal=True))
            return LoadJobResult(
                execution_run_id="11111111-1111-4111-8111-111111111111",
                verification_complete=True,
            )

        queued = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Production",
            total_rows=3,
            access_context=_access_context(),
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
        self.assertEqual(
            [item.status for item in published],
            [LoadJobStatus.RUNNING, LoadJobStatus.SUCCEEDED],
        )
        self.assertEqual(
            manager.latest_many((WORKSPACE_ID,))[WORKSPACE_ID],
            finished,
        )

    def test_duplicate_submission_reuses_the_active_job(self) -> None:
        manager = LoadJobManager()
        started = Event()
        release = Event()

        def work(_access_context, _report_writing, _report_verifying):
            started.set()
            release.wait(1)
            return LoadJobResult(execution_run_id="run-1", verification_complete=False)

        first = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            access_context=_access_context(),
            work=work,
        )
        self.assertTrue(started.wait(1))
        with self.assertRaises(MigrationIdentifierConfusionError):
            manager.enqueue(
                WORKSPACE_ID,
                "Customer migration",
                target_database="migration",
                target_server="odoo.example.test",
                target_environment="Test",
                total_rows=3,
                access_context=replace(
                    _access_context(),
                    data_version_id="30000000-0000-4000-8000-000000000002",
                ),
                work=work,
            )
        duplicate = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            access_context=_access_context(),
            work=work,
        )
        release.set()
        _wait_for_terminal(manager, first.job_id)
        manager.shutdown()

        self.assertEqual(duplicate.job_id, first.job_id)

    def test_unexpected_failure_does_not_expose_internal_detail(self) -> None:
        manager = LoadJobManager()

        def work(_access_context, _report_writing, _report_verifying):
            raise RuntimeError("secret implementation detail")

        queued = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            access_context=_access_context(),
            work=work,
        )
        failed = _wait_for_terminal(manager, queued.job_id)
        manager.shutdown()

        self.assertEqual(failed.status.value, "FAILED")
        self.assertNotIn("secret implementation detail", failed.failure_message)
        self.assertIn("stopped the load", failed.failure_message)


if __name__ == "__main__":
    unittest.main()
