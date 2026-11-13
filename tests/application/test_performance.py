"""Verify isolated worker observations without changing operation outcomes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.application.shared.performance import (
    OperationPerformance,
    bind_performance,
    current_performance,
    performance_count,
    performance_merge,
    performance_phase,
)


class OperationPerformanceTests(unittest.TestCase):
    def test_phase_clocks_and_external_cpu_availability_are_explicit(self):
        events = []
        with (
            patch(
                "impodo.application.shared.performance.perf_counter",
                side_effect=[1, 2, 3.5, 5],
            ),
            patch(
                "impodo.application.shared.performance.thread_time",
                side_effect=[0.1, 0.2, 0.6, 0.9],
            ),
        ):
            collector = OperationPerformance(
                operation_id=str(uuid4()),
                operation="preflight",
                sink=events.append,
                queue_wait_ms=50,
            )
            with collector.phase("compare"):
                collector.count("prepared_rows", 100)
            collector.record_phase("manifest_serialization", 5)
            collector.finish(outcome="succeeded")
        terminal = events[-1]
        self.assertEqual(terminal["wall_ms"], 4000)
        self.assertEqual(terminal["coordinator_thread_cpu_ms"], 800)
        self.assertEqual(terminal["queue_wait_ms"], 50)
        self.assertEqual(terminal["phases"]["compare"]["wall_ms"], 1500)
        self.assertEqual(terminal["phases"]["compare"]["thread_cpu_ms"], 400)
        self.assertIsNone(terminal["phases"]["manifest_serialization"]["thread_cpu_ms"])
        self.assertEqual(terminal["counters"]["prepared_rows"], 100)
        self.assertIn("prepared_rows", terminal["coverage"]["counters"])
        self.assertNotIn("rpc_retries", terminal["coverage"]["counters"])

    def test_workers_return_immutable_isolated_snapshots_and_merge_explicitly(self):
        collector = OperationPerformance(
            operation_id=str(uuid4()), operation="preflight"
        )

        def work(child, rows):
            self.assertIsNone(current_performance())
            with bind_performance(child), performance_phase("read_record_batch"):
                performance_count("rpc_rows", rows)
                performance_count("rpc_calls")
            return child.snapshot()

        with bind_performance(collector), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(work, collector.worker(), rows) for rows in (10, 20)
            ]
            snapshots = [future.result() for future in futures]
            self.assertEqual(collector.snapshot().counters, ())
            for snapshot in reversed(snapshots):
                performance_merge(snapshot)
        result = collector.snapshot()
        self.assertEqual(dict(result.counters), {"rpc_rows": 30, "rpc_calls": 2})
        self.assertEqual(result.phases[0].calls, 2)
        with self.assertRaises(FrozenInstanceError):
            result.observed_workers = 7
        self.assertIsNone(current_performance())

    def test_capture_failure_preserves_original_error_and_terminal_is_once(self):
        def fail_sink(_payload):
            raise OSError("unavailable diagnostics")

        collector = OperationPerformance(
            operation_id=str(uuid4()), operation="preflight", sink=fail_sink
        )
        with self.assertRaisesRegex(ValueError, "business failure"):
            with bind_performance(collector), performance_phase("compare"):
                raise ValueError("business failure")
        collector.finish(outcome="failed", failure_code="ODOO_UNREACHABLE")
        collector.finish(outcome="succeeded")
        self.assertIsNone(current_performance())

    def test_resource_probe_failure_and_unknown_names_do_not_change_work(self):
        events = []
        with patch(
            "impodo.application.shared.performance.psutil.Process",
            side_effect=RuntimeError("unsupported probe"),
        ):
            collector = OperationPerformance(
                operation_id=str(uuid4()), operation="preparation", sink=events.append
            )
            with collector.phase("private source value"):
                collector.count("customer name", 100)
                collector.metrics({"database_connection_ms": 4, "private value": 99})
            collector.observe_workers(0)
            collector.finish(outcome="succeeded")
        terminal = events[-1]
        self.assertEqual(terminal["capture_failure_count"], 2)
        self.assertIsNone(terminal["peak_process_rss_bytes"])
        self.assertEqual(terminal["observed_workers"], 0)
        self.assertEqual(terminal["phases"], {})
        self.assertEqual(terminal["counters"], {})
        self.assertEqual(terminal["metrics"], {"database_connection_ms": 4})
        self.assertNotIn("private", str(terminal))
