"""Keep comparison telemetry separate from job truth and durable receipts."""

from collections.abc import Mapping
from threading import Event
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import Mock

from impodo.application.preflight_attempts import (
    PreflightAttemptStore,
    PreflightComparisonReceipt,
)
from impodo.application.preflight_jobs import PreflightJobManager, PreflightJobResult
from impodo.application.preflight_timing import timed_preflight_publication
from impodo.application.shared.performance import (
    OperationPerformance,
    bind_performance,
    current_performance,
    performance_count,
    performance_phase,
)
from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.domain.odoo.contracts import ConnectorTransportError

PROJECT = "10000000-0000-4000-8000-000000000001"
WORKSPACE = "20000000-0000-4000-8000-000000000001"
DATA_VERSION = "30000000-0000-4000-8000-000000000001"
RUN = "40000000-0000-4000-8000-000000000001"
RESULT = "50000000-0000-4000-8000-000000000001"
PARENT = "60000000-0000-4000-8000-000000000001"
HASH = "sha256:" + "1" * 64


def access(workspace=WORKSPACE):
    return WorkspaceAccessContext(
        project_id=PROJECT,
        workspace_id=workspace,
        data_version_id=DATA_VERSION,
        migration_run_id=RUN,
    )


def result(receipt=None):
    return PreflightJobResult(RESULT, "/summary", "Comparison saved", receipt)


def terminal(manager, workspace, job_id):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        job = manager.get(workspace, job_id)
        if job.terminal:
            return job
        sleep(0.005)
    raise AssertionError("The comparison worker did not finish")


class PreflightPerformanceJobTests(TestCase):
    def test_worker_owns_its_collector_and_reports_publication_after_finalization(self):
        events = []
        completed = Event()
        store = Mock(spec=PreflightAttemptStore)
        committed = Event()

        def commit(*_args, **_kwargs):
            committed.set()

        store.commit_preflight_comparison.side_effect = commit

        def record(value: Mapping[str, object]):
            events.append(dict(value))
            if value["event"] == "performance_operation_completed":
                self.assertTrue(committed.is_set())
                completed.set()

        manager = PreflightJobManager(
            store, performance_sink=record, configured_read_workers=2
        )
        self.addCleanup(manager.shutdown)
        parent = OperationPerformance(operation_id=PARENT, operation="preflight")

        def work(_progress):
            collector = current_performance()
            self.assertIsNotNone(collector)
            self.assertIsNot(collector, parent)
            self.assertNotEqual(collector.operation_id, PARENT)
            with (
                performance_phase("read_odoo"),
                timed_preflight_publication("manifest_serialization"),
            ):
                performance_count("prepared_rows", 12)
            receipt = PreflightComparisonReceipt(
                collector.operation_id,
                WORKSPACE,
                RESULT,
                HASH,
                HASH,
                HASH,
                HASH,
            )
            return result(receipt)

        with bind_performance(parent):
            job = manager.enqueue(
                WORKSPACE, "Fictional project", access_context=access(), work=work
            )
            self.assertTrue(completed.wait(timeout=5))
            self.assertIs(current_performance(), parent)
        finished = terminal(manager, WORKSPACE, job.job_id)
        self.assertEqual(finished.status.value, "SUCCEEDED")
        summary = events[-1]
        self.assertEqual(summary["operation_id"], job.job_id)
        self.assertEqual(summary["configured_workers"], 2)
        self.assertEqual(summary["outcome"], "succeeded")
        self.assertGreaterEqual(summary["queue_wait_ms"], 0)
        self.assertEqual(summary["counters"]["prepared_rows"], 12)
        self.assertIn("finalize", summary["phases"])
        self.assertIn("manifest_serialization", summary["phases"])
        self.assertIsNone(summary["phases"]["manifest_serialization"]["thread_cpu_ms"])
        self.assertEqual(dict(parent.snapshot().counters), {})
        self.assertIsNone(current_performance())
        self.assertNotIn("Fictional project", repr(events))
        self.assertEqual(
            store.commit_preflight_comparison.call_args.args, (job.job_id, WORKSPACE)
        )

    def test_failing_recorder_does_not_change_success_or_stop_the_next_job(self):
        def record(_value):
            raise OSError("Fictional unavailable diagnostics disk")

        manager = PreflightJobManager(performance_sink=record)
        self.addCleanup(manager.shutdown)
        for _ in range(2):
            job = manager.enqueue(
                WORKSPACE,
                "Fictional",
                access_context=access(),
                work=lambda _progress: result(),
            )
            self.assertEqual(
                terminal(manager, WORKSPACE, job.job_id).status.value, "SUCCEEDED"
            )

    def test_failed_read_keeps_safe_failure_and_observed_phase(self):
        events, completed = [], Event()

        def record(value):
            events.append(dict(value))
            if value["event"] == "performance_operation_completed":
                completed.set()

        def work(_progress):
            with performance_phase("read_odoo"):
                raise ConnectorTransportError("HTTP 503 confidential-target-detail")

        manager = PreflightJobManager(performance_sink=record)
        self.addCleanup(manager.shutdown)
        job = manager.enqueue(
            WORKSPACE, "Fictional", access_context=access(), work=work
        )
        self.assertTrue(completed.wait(timeout=5))
        finished = terminal(manager, WORKSPACE, job.job_id)
        self.assertEqual(finished.status.value, "FAILED")
        self.assertEqual(events[-1]["failure_code"], finished.failure.code.value)
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertIn("read_odoo", events[-1]["phases"])
        self.assertNotIn("confidential-target-detail", repr(events))

    def test_legacy_opaque_operation_id_does_not_change_job_truth_or_enter_diagnostics(
        self,
    ):
        events, completed = [], Event()

        def record(value):
            events.append(dict(value))
            if value["event"] == "performance_operation_completed":
                completed.set()

        manager = PreflightJobManager(performance_sink=record)
        self.addCleanup(manager.shutdown)
        opaque = "legacy-opaque-operation-customer-sensitive"
        job = manager.enqueue(
            WORKSPACE,
            "Fictional",
            access_context=access(),
            operation_id=opaque,
            work=lambda _progress: result(),
        )
        self.assertTrue(completed.wait(timeout=5))
        self.assertEqual(
            terminal(manager, WORKSPACE, job.job_id).status.value, "SUCCEEDED"
        )
        self.assertEqual(job.job_id, opaque)
        self.assertNotIn(opaque, repr(events))
        self.assertEqual(events[-1]["outcome"], "succeeded")

    def test_pending_job_reports_interruption_without_running_its_work(self):
        started, release = Event(), Event()
        events = []
        manager = PreflightJobManager(
            performance_sink=lambda value: events.append(dict(value))
        )
        self.addCleanup(manager.shutdown)
        self.addCleanup(release.set)

        def first(_progress):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return result()

        running = manager.enqueue(
            WORKSPACE, "First", access_context=access(), work=first
        )
        self.assertTrue(started.wait(timeout=5))
        second_workspace = "20000000-0000-4000-8000-000000000002"
        pending_work = Mock(return_value=result())
        pending = manager.enqueue(
            second_workspace,
            "Second",
            access_context=access(second_workspace),
            work=pending_work,
        )
        manager.shutdown()
        interrupted = [
            event for event in events if event.get("operation_id") == pending.job_id
        ]
        self.assertEqual(interrupted[-1]["outcome"], "interrupted")
        self.assertGreaterEqual(interrupted[-1]["queue_wait_ms"], 0)
        self.assertEqual(
            manager.get(second_workspace, pending.job_id).status.value, "FAILED"
        )
        pending_work.assert_not_called()
        release.set()
        self.assertEqual(
            terminal(manager, WORKSPACE, running.job_id).status.value, "SUCCEEDED"
        )
