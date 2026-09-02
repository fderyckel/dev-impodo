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
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.project.foundation import MigrationIdentifierConfusionError
from impodo.application.workspace.execution.job_models import LoadJobStatus, LoadPhase
from impodo.application.workspace.access import WorkspaceAccessContext


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
            schedule_component=0,
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
            schedule_component=0,
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
            schedule_component=1,
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
                completion_warning="Correction origin is unavailable.",
                completion_warning_code="CORRECTION_ORIGIN_PREPARED_MISSING",
            )

        queued = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Production",
            total_rows=3,
            relationship_total_rows=1,
            load_group_count=2,
            access_context=_access_context(),
            work=work,
        )
        finished = _wait_for_terminal(manager, queued.job_id)
        manager.shutdown()

        self.assertEqual(finished.status.value, "SUCCEEDED")
        self.assertEqual(finished.completed_rows, 2)
        self.assertEqual(finished.created_count, 2)
        self.assertEqual(finished.updated_count, 1)
        self.assertEqual(finished.relationship_pending_count, 1)
        self.assertEqual(finished.relationship_total_count, 1)
        self.assertEqual(finished.relationship_completed_count, 0)
        self.assertEqual(finished.load_group_number, 2)
        self.assertEqual(finished.load_group_count, 2)
        self.assertEqual(finished.attention_count, 1)
        self.assertEqual(finished.progress_percent, 100)
        self.assertTrue(finished.verification_complete)
        self.assertEqual(
            finished.completion_warning,
            "Correction origin is unavailable.",
        )
        self.assertEqual(
            finished.completion_warning_code,
            "CORRECTION_ORIGIN_PREPARED_MISSING",
        )
        self.assertEqual(
            [item.status for item in published],
            [LoadJobStatus.RUNNING, LoadJobStatus.SUCCEEDED],
        )
        self.assertEqual(
            manager.latest_many((WORKSPACE_ID,))[WORKSPACE_ID],
            finished,
        )

    def test_reports_relationship_work_after_first_pass_writes(self) -> None:
        manager = LoadJobManager()
        relationship_phase = Event()
        release = Event()

        def work(_access_context, report_writing, report_verifying):
            report_writing(_run(terminal=False))
            relationship_phase.set()
            release.wait(1)
            report_verifying(_run(terminal=True))
            return LoadJobResult(
                execution_run_id="11111111-1111-4111-8111-111111111111",
                verification_complete=False,
            )

        queued = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            relationship_total_rows=1,
            load_group_count=2,
            access_context=_access_context(),
            work=work,
        )
        self.assertTrue(relationship_phase.wait(1))

        active = manager.get(WORKSPACE_ID, queued.job_id)

        self.assertEqual(active.phase, LoadPhase.RELATIONSHIPS)
        self.assertEqual(active.completed_rows, 2)
        self.assertEqual(active.created_count, 2)
        self.assertEqual(active.relationship_pending_count, 1)
        self.assertEqual(active.relationship_total_count, 1)
        self.assertEqual(active.relationship_completed_count, 0)
        self.assertEqual(active.load_group_number, 2)
        self.assertEqual(active.load_group_count, 2)
        self.assertGreaterEqual(active.progress_percent, 82)
        self.assertLess(active.progress_percent, 90)
        release.set()
        _wait_for_terminal(manager, queued.job_id)
        manager.shutdown()

    def test_in_flight_row_is_not_reported_as_final_or_as_relationship_work(
        self,
    ) -> None:
        manager = LoadJobManager()
        writing_phase = Event()
        release = Event()
        run = _run(terminal=False)
        in_flight = replace(
            run,
            rows=(
                replace(
                    run.rows[0],
                    status=ExecutionRowStatus.IN_FLIGHT,
                    transport_page=0,
                    transport_batch=2,
                    transport_phase="CREATE",
                ),
                *run.rows[1:],
            ),
        )

        def work(_access_context, report_writing, _report_verifying):
            report_writing(in_flight)
            writing_phase.set()
            release.wait(1)
            return LoadJobResult(
                execution_run_id=in_flight.run_id,
                verification_complete=False,
            )

        queued = manager.enqueue(
            WORKSPACE_ID,
            "Customer migration",
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            relationship_total_rows=1,
            load_group_count=2,
            access_context=_access_context(),
            work=work,
        )
        self.assertTrue(writing_phase.wait(1))

        active = manager.get(WORKSPACE_ID, queued.job_id)

        self.assertEqual(active.phase, LoadPhase.WRITING)
        self.assertEqual(active.completed_rows, 1)
        self.assertEqual(active.load_group_number, 1)
        self.assertLess(active.progress_percent, 82)
        release.set()
        _wait_for_terminal(manager, queued.job_id)
        manager.shutdown()

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
