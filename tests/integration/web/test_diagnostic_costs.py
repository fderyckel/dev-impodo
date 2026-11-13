"""Keep diagnostic cost accounting bounded, private and independent of requests."""

from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from impodo.application.shared.io_performance import measure_file_io
from impodo.application.shared.worker_startup import record_worker_startup
from impodo.web.diagnostic_costs import (
    MAX_LOGGING_CORRELATIONS,
    DiagnosticCostTracker,
    logging_correlations,
)
from impodo.web.diagnostics import LocalDiagnosticRecorder, RequestDiagnosticsMiddleware


class DiagnosticCostsTests(unittest.TestCase):
    def test_cost_correlations_are_bounded_detached_and_uuid_only(self):
        tracker = DiagnosticCostTracker()
        total = MAX_LOGGING_CORRELATIONS + 10
        for _ in range(total):
            with logging_correlations({"request_id": str(uuid4())}):
                tracker.record({"diagnostic_write_count": 1, "source_value": 12})
        with logging_correlations({"request_id": "private-customer-name"}):
            tracker.record({"diagnostic_write_count": 1})
        snapshot = tracker.snapshot()
        self.assertEqual(len(snapshot["correlations"]), MAX_LOGGING_CORRELATIONS)
        self.assertEqual(snapshot["dropped_correlation_count"], 10)
        self.assertEqual(snapshot["metrics"]["diagnostic_write_count"], total + 1)
        self.assertNotIn("source_value", json.dumps(snapshot))
        self.assertNotIn("private-customer-name", json.dumps(snapshot))
        snapshot["metrics"]["diagnostic_write_count"] = 0
        self.assertEqual(
            tracker.snapshot()["metrics"]["diagnostic_write_count"], total + 1
        )

    def test_concurrent_log_writes_keep_exact_counts_and_actual_bytes(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:

                def record(_index):
                    recorder.record_request(
                        request_id=str(uuid4()),
                        method="GET",
                        route_template="/health",
                        status_code=200,
                        duration_ms=1,
                    )

                with ThreadPoolExecutor(max_workers=3) as executor:
                    list(executor.map(record, range(30)))
                snapshot = recorder.logging_costs_snapshot()
                raw = recorder.path.read_bytes()
                metrics = snapshot["metrics"]
                self.assertEqual(metrics["diagnostic_write_count"], 30)
                self.assertEqual(metrics["diagnostic_write_attempt_count"], 30)
                self.assertEqual(metrics["diagnostic_bytes_written"], len(raw))
                self.assertEqual(len(raw.splitlines()), 30)
                self.assertTrue(snapshot["capture_complete"])
                self.assertEqual(snapshot["child_process_coverage"], "unavailable")
                for field in (
                    "diagnostic_encode_ms",
                    "diagnostic_thread_lock_wait_ms",
                    "diagnostic_file_lock_wait_ms",
                    "diagnostic_emit_ms",
                    "diagnostic_open_ms",
                    "diagnostic_write_flush_ms",
                    "diagnostic_close_ms",
                ):
                    self.assertGreaterEqual(metrics[field], 0)
            finally:
                recorder.close()

    def test_logging_measurement_failure_does_not_drop_the_business_log(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:
                with patch.object(
                    recorder._logging_costs, "record", side_effect=RuntimeError
                ):
                    recorder.record_lifecycle("application_started")
                raw = recorder.path.read_text()
                self.assertEqual(json.loads(raw)["event"], "application_started")
                self.assertEqual(len(raw.splitlines()), 1)
                self.assertFalse(recorder.logging_costs_snapshot()["capture_complete"])
            finally:
                recorder.close()

    def test_optional_clock_failure_keeps_log_counts_without_invented_timings(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:
                with patch(
                    "impodo.web.diagnostic_costs.perf_counter", side_effect=RuntimeError
                ):
                    recorder.record_lifecycle("application_started")
                snapshot = recorder.logging_costs_snapshot()
                self.assertEqual(snapshot["metrics"]["diagnostic_write_count"], 1)
                self.assertNotIn("diagnostic_encode_ms", snapshot["metrics"])
                self.assertFalse(snapshot["capture_complete"])
                self.assertEqual(
                    json.loads(recorder.path.read_text())["event"],
                    "application_started",
                )
            finally:
                recorder.close()

    def test_outer_logger_failure_has_a_safe_cost_and_capture_failure_count(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:
                with patch.object(
                    recorder._logger,
                    "info",
                    side_effect=OSError("private exception detail"),
                ):
                    recorder.record_lifecycle("application_started")
                snapshot = recorder.logging_costs_snapshot()
                self.assertEqual(
                    snapshot["metrics"]["diagnostic_capture_failure_count"], 1
                )
                self.assertEqual(recorder.capture_failure_count, 1)
                self.assertNotIn("private exception detail", json.dumps(snapshot))
            finally:
                recorder.close()

    def test_log_lock_timeout_is_measured_without_raising_or_retrying_a_write(self):
        @contextmanager
        def unavailable(_stream):
            raise TimeoutError("fixture lock timeout")
            yield

        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:
                with patch(
                    "impodo.web.diagnostics._exclusive_process_file_lock", unavailable
                ):
                    recorder.record_lifecycle("application_started")
                metrics = recorder.logging_costs_snapshot()["metrics"]
                self.assertEqual(metrics["diagnostic_lock_timeout_count"], 1)
                self.assertEqual(metrics["diagnostic_capture_failure_count"], 1)
                self.assertEqual(metrics.get("diagnostic_write_count", 0), 0)
                self.assertGreaterEqual(metrics["diagnostic_file_lock_wait_ms"], 0)
                self.assertEqual(recorder.capture_failure_count, 1)
            finally:
                recorder.close()

    def test_rotation_and_load_recorder_costs_remain_separate(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory, max_bytes=150, backup_count=2)
            try:
                for _ in range(8):
                    recorder.record_lifecycle("application_started")
                observer = recorder.load_observation(
                    action="LOAD", request_id=str(uuid4())
                )
                observer.emit("terminal", outcome="COMPLETED", reason="")
                snapshot = recorder.logging_costs_snapshot()
                self.assertGreater(snapshot["metrics"]["diagnostic_rotation_count"], 0)
                self.assertIn("events", snapshot["load_recorders"])
                self.assertNotIn(str(Path(directory)), json.dumps(snapshot))
                self.assertTrue(snapshot["component_timings_may_overlap"])
            finally:
                recorder.close()


class RequestCostAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_collects_file_startup_and_post_log_lifecycle_without_extra_logs(
        self,
    ):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:

                async def app(scope, receive, send):
                    with measure_file_io("read") as transfer:
                        if transfer is not None:
                            transfer.add_bytes(25)
                    record_worker_startup(
                        "inspection", worker_count=1, start_call_ms=2.0
                    )
                    await send(
                        {"type": "http.response.start", "status": 200, "headers": []}
                    )
                    await send({"type": "http.response.body", "body": b"ok"})

                async def receive():
                    return {"type": "http.disconnect"}

                async def send(message):
                    pass

                middleware = RequestDiagnosticsMiddleware(app, recorder=recorder)
                await middleware(
                    {"type": "http", "method": "GET", "path": "/health", "state": {}},
                    receive,
                    send,
                )
                records = [
                    json.loads(line) for line in recorder.path.read_text().splitlines()
                ]
                self.assertEqual(len(records), 1)
                request = records[0]
                self.assertEqual(request["file_io"]["read_bytes"], 25)
                self.assertEqual(
                    request["worker_startup"]["operations"]["inspection"][
                        "worker_count"
                    ],
                    1,
                )
                costs = recorder.logging_costs_snapshot()
                request_cost = next(
                    item
                    for item in costs["correlations"]
                    if item["kind"] == "request" and item["id"] == request["request_id"]
                )
                metrics = request_cost["metrics"]
                self.assertAlmostEqual(
                    metrics["request_reported_ms"], request["duration_ms"], places=2
                )
                self.assertGreaterEqual(
                    metrics["request_lifecycle_ms"], metrics["request_reported_ms"]
                )
                self.assertEqual(metrics["request_completed_count"], 1)
            finally:
                recorder.close()

    async def test_failed_optional_observer_preserves_response_and_original_app_failure(
        self,
    ):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            try:

                async def app(scope, receive, send):
                    raise LookupError("business failure")

                async def noop(*args):
                    return None

                middleware = RequestDiagnosticsMiddleware(app, recorder=recorder)
                with (
                    patch(
                        "impodo.web.diagnostics.collect_file_io",
                        side_effect=RuntimeError,
                    ),
                    self.assertRaisesRegex(LookupError, "business failure"),
                ):
                    await middleware(
                        {
                            "type": "http",
                            "method": "GET",
                            "path": "/health",
                            "state": {},
                        },
                        noop,
                        noop,
                    )
                self.assertGreater(recorder.capture_failure_count, 0)
                request = json.loads(recorder.path.read_text())
                self.assertEqual(request["exception_class"], "LookupError")
                self.assertNotIn("file_io", request)
            finally:
                recorder.close()
