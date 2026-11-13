"""Exercise performance export privacy, coverage and retained-job summaries."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from zipfile import ZipFile

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.shared.performance import OperationPerformance
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    LocalDiagnosticRecorder,
    create_diagnostic_bundle,
)
from impodo.web.performance_diagnostics import performance_summary


class PerformanceDiagnosticTests(unittest.TestCase):
    def test_job_metrics_survive_capture_and_export_resanitization(self):
        operation_id = str(uuid4())
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            collector = OperationPerformance(
                operation_id=operation_id,
                operation="preflight",
                sink=recorder.record_performance,
                configured_workers=2,
            )
            with collector.phase("read_odoo"):
                collector.count("rpc_calls", 4)
                collector.count("rpc_rows", 100)
                collector.observe_workers(2)
            collector.record_phase("manifest_serialization", 12)
            collector.finish(outcome="succeeded")
            recorder.record_request(
                request_id="status-request",
                method="GET",
                route_template="/workspaces/{workspace_id}/preflight/{job_id}/status",
                status_code=200,
                duration_ms=7,
                performance_operation_id=operation_id,
            )
            recorder.close()
            log = Path(directory) / DIAGNOSTIC_LOG_NAME
            records = [json.loads(line) for line in log.read_text().splitlines()]
            for record in records:
                record["customer_name"] = "private customer value"
                if record.get("event") == "performance_operation_completed":
                    record["counters"]["private counter"] = "private source value"
                    record["phases"]["private phase"] = {"wall_ms": 99}
                    record["runtime"]["hostname"] = "private hostname"
                    record["runtime"]["operating_system_release"] = "private hostname"
            log.write_text("\n".join(json.dumps(record) for record in records) + "\n")
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory, build_contract=PROCESS_BUILD_CONTRACT
                    )
                )
            ) as archive:
                exported = archive.read("diagnostics.jsonl").decode()
                summary = json.loads(archive.read("performance-summary.json"))
                manifest = json.loads(archive.read("manifest.json"))
        attempt = summary["attempts"][0]
        self.assertTrue(attempt["terminal_observed"])
        self.assertEqual(attempt["operation_id"], operation_id)
        self.assertEqual(
            attempt["application_build_id"], PROCESS_BUILD_CONTRACT.application_build_id
        )
        self.assertEqual(attempt["counters"], {"rpc_calls": 4, "rpc_rows": 100})
        self.assertEqual(attempt["configured_workers"], 2)
        self.assertEqual(attempt["observed_workers"], 2)
        self.assertIsNone(attempt["phases"]["manifest_serialization"]["thread_cpu_ms"])
        self.assertEqual(manifest["runtime"]["memory_scope"], "system_wide")
        self.assertIn("logical_cpu_count", manifest["runtime"])
        self.assertIn(operation_id, exported)
        self.assertNotIn("private", exported)
        self.assertNotIn("private", json.dumps(summary))
        self.assertIn("unavailable", summary["browser_timing"])

    def test_incomplete_and_many_attempts_have_explicit_bounded_coverage(self):
        records = [
            {
                "event": "performance_phase_completed",
                "operation": "preflight",
                "operation_id": str(uuid4()),
                "phase": "read_records",
                "wall_ms": 100,
                "recorded_at": "2026-11-13T12:00:00Z",
            }
            for _ in range(100)
        ]
        summary = performance_summary(records)
        self.assertEqual(len(summary["attempts"]), 30)
        self.assertTrue(
            all(not item["terminal_observed"] for item in summary["attempts"])
        )
        self.assertIn("missing", summary["coverage"])
        self.assertNotIn("outcome", summary["attempts"][0])

    def test_request_polling_does_not_evict_retained_active_or_terminal_evidence(self):
        completed_id, active_id = str(uuid4()), str(uuid4())
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            completed = OperationPerformance(
                operation_id=completed_id, operation="preflight",
                sink=recorder.record_performance,
            )
            completed.count("rpc_rows", 150)
            completed.finish(outcome="succeeded")
            active = OperationPerformance(
                operation_id=active_id, operation="preflight",
                sink=recorder.record_performance,
            )
            with active.phase("read_odoo"):
                for _ in range(1_025):
                    recorder.record_performance({
                        "event": "performance_phase_completed",
                        "operation_id": active_id, "operation": "preflight",
                        "phase": "read_record_batch", "wall_ms": 1,
                        "thread_cpu_ms": 0.1, "calls": 1,
                    })
                for index in range(100):
                    recorder.record_request(
                        request_id=f"poll-{index}", method="GET",
                        route_template="/workspaces/{workspace_id}/preflight/{job_id}/status",
                        status_code=200, duration_ms=2,
                        performance_operation_id=active_id,
                    )
                with ZipFile(BytesIO(create_diagnostic_bundle(
                    directory, build_contract=PROCESS_BUILD_CONTRACT, max_records=3,
                ))) as archive:
                    summary = json.loads(archive.read("performance-summary.json"))
                    general = [json.loads(line) for line in archive.read("diagnostics.jsonl").splitlines()]
                    manifest = json.loads(archive.read("manifest.json"))
            recorder.close()
        attempts = {item["operation_id"]: item for item in summary["attempts"]}
        self.assertEqual(set(attempts), {completed_id, active_id})
        self.assertTrue(attempts[completed_id]["terminal_observed"])
        self.assertEqual(attempts[completed_id]["counters"], {"rpc_rows": 150})
        self.assertFalse(attempts[active_id]["terminal_observed"])
        self.assertEqual(attempts[active_id]["phase_in_progress"], "read_odoo")
        self.assertEqual(attempts[active_id]["phases"]["read_record_batch"]["calls"], 1_025)
        self.assertEqual(len(general), 3)
        self.assertTrue(all(item["event"] == "request_completed" for item in general))
        self.assertEqual(manifest["record_count"], 3)
        self.assertGreater(manifest["coverage"]["general"]["omitted_record_count"], 0)

    def test_partial_attempt_keeps_start_and_accumulates_completed_phases(self):
        operation_id = str(uuid4())
        records = [
            {
                "event": "performance_operation_started",
                "operation": "preflight",
                "operation_id": operation_id,
                "configured_workers": 2,
                "recorded_at": "2026-11-13T12:00:00Z",
            }
        ]
        for duration in (10, 20):
            records.append(
                {
                    "event": "performance_phase_completed",
                    "operation": "preflight",
                    "operation_id": operation_id,
                    "phase": "verify",
                    "wall_ms": duration,
                    "thread_cpu_ms": 1,
                    "calls": 1,
                    "recorded_at": "2026-11-13T12:00:01Z",
                }
            )
        attempt = performance_summary(records)["attempts"][0]
        self.assertFalse(attempt["terminal_observed"])
        self.assertEqual(
            attempt["phases"]["verify"], {"wall_ms": 30, "thread_cpu_ms": 2, "calls": 2}
        )
        self.assertEqual(attempt["configured_workers"], 2)
        self.assertEqual(
            attempt["slowest_phases"], [{"phase": "verify", "wall_ms": 30}]
        )
        started = performance_summary(records[:1])["attempts"][0]
        self.assertFalse(started["terminal_observed"])
        self.assertNotIn("outcome", started)

    def test_failed_job_retains_counts_without_exception_message(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            collector = OperationPerformance(
                operation_id=str(uuid4()),
                operation="preflight",
                sink=recorder.record_performance,
            )
            collector.count("rpc_failures", 1)
            collector.finish(outcome="failed", failure_code="ODOO_ACCESS_DENIED")
            recorder.close()
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory, build_contract=PROCESS_BUILD_CONTRACT
                    )
                )
            ) as archive:
                summary = json.loads(archive.read("performance-summary.json"))
        self.assertEqual(summary["attempts"][0]["outcome"], "failed")
        self.assertEqual(summary["attempts"][0]["failure_code"], "ODOO_ACCESS_DENIED")
        self.assertEqual(summary["attempts"][0]["counters"], {"rpc_failures": 1})

    def test_preparation_and_comparison_share_the_summary_with_separate_cost_scopes(
        self,
    ):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            preparation_id = str(uuid4())
            for kind, extra in (
                ("attempt_started", {}),
                ("phase_completed", {"phase": "transformation", "duration_ms": 90}),
                (
                    "attempt_finished",
                    {
                        "outcome": "succeeded",
                        "measurement_complete": True,
                        "duration_ms": 120,
                    },
                ),
            ):
                recorder.record_performance_event(
                    {
                        "schema_version": 1,
                        "attempt_id": preparation_id,
                        "operation": "preparation",
                        "event_kind": kind,
                        "environment": {
                            "build_id": PROCESS_BUILD_CONTRACT.application_build_id
                        },
                        "configuration": {"polars_threads_effective": 2},
                        "resources": {"cpu_seconds": 0.05, "sampling_complete": True},
                        "private": "private source value",
                        **extra,
                    }
                )
            comparison = OperationPerformance(
                operation_id=str(uuid4()),
                operation="preflight",
                sink=recorder.record_performance,
            )
            comparison.finish(outcome="succeeded")
            recorder.close()
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory, build_contract=PROCESS_BUILD_CONTRACT
                    )
                )
            ) as archive:
                summary = json.loads(archive.read("performance-summary.json"))
        self.assertEqual(
            {item["operation"] for item in summary["attempts"]},
            {"preparation", "preflight"},
        )
        preparation = next(
            item for item in summary["attempts"] if item["operation"] == "preparation"
        )
        self.assertTrue(preparation["terminal_observed"])
        self.assertEqual(preparation["phases"]["transformation"]["wall_ms"], 90)
        self.assertEqual(preparation["configuration"]["polars_threads_effective"], 2)
        self.assertIsNone(preparation["phases"]["transformation"]["thread_cpu_ms"])
        self.assertEqual(preparation["resources"]["cpu_seconds"], 0.05)
        self.assertNotIn("private", json.dumps(summary))

    def test_stalled_phase_is_visible_and_terminal_clears_it(self):
        collector_events = []
        collector = OperationPerformance(
            operation_id=str(uuid4()),
            operation="preflight",
            sink=collector_events.append,
        )
        with collector.phase("read_odoo"):
            attempt = performance_summary(collector_events)["attempts"][0]
            self.assertEqual(attempt["phase_in_progress"], "read_odoo")
            self.assertFalse(attempt["terminal_observed"])
        collector.finish(outcome="succeeded")
        attempt = performance_summary(collector_events)["attempts"][0]
        self.assertIsNone(attempt["phase_in_progress"])
        self.assertTrue(attempt["terminal_observed"])
