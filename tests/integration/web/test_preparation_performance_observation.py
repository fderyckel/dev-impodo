"""Bound supervisor sampling and preserve optional, value-free evidence."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import psutil

from impodo.web.composition.preparation_performance import (
    MAX_RETAINED_PHASES,
    PreparationPerformanceObservation,
    execution_environment,
)

MODULE = "impodo.web.composition.preparation_performance"
BUILD = "sha256:" + "a" * 64


class PreparationPerformanceObservationTests(unittest.TestCase):
    def test_queue_and_worker_startup_are_distinct_non_overlapping_intervals(self):
        clock = [10.0]
        records = []
        with (
            patch(f"{MODULE}.execution_environment", return_value={"build_id": BUILD}),
            patch(f"{MODULE}.perf_counter", side_effect=lambda: clock[0]),
            patch.object(PreparationPerformanceObservation, "sample"),
        ):
            observer = PreparationPerformanceObservation(
                str(uuid4()), 12, 7.0, records.append
            )
            observer.start(None, BUILD)
            observer.attach_worker_pid(1234)
            clock[0] = 12.0
            observer.worker_started()
            clock[0] = 15.0
            observer.finish("succeeded", True)
        phases = {
            item.get("phase"): item
            for item in records
            if item["event_kind"] == "phase_completed"
        }
        self.assertEqual(phases["queue_wait"]["duration_ms"], 3000)
        self.assertEqual(phases["worker_startup"]["duration_ms"], 2000)
        self.assertEqual(phases["worker_startup"]["timing_scope"], "exclusive")
        self.assertEqual(records[-1]["duration_ms"], 8000)

    def test_samples_at_most_once_per_second_and_keeps_same_sample_combined_peak(self):
        clock = [1.0]
        parent = Mock()
        worker = Mock()
        parent.memory_info.side_effect = [
            SimpleNamespace(rss=100),
            SimpleNamespace(rss=80),
        ]
        worker.memory_info.side_effect = [
            SimpleNamespace(rss=40),
            SimpleNamespace(rss=90),
        ]
        parent.cpu_times.side_effect = [
            SimpleNamespace(user=1.0, system=0.2),
            SimpleNamespace(user=1.2, system=0.3),
        ]
        worker.cpu_times.side_effect = [
            SimpleNamespace(user=0.2, system=0.1),
            SimpleNamespace(user=0.4, system=0.2),
        ]
        with (
            patch(f"{MODULE}.execution_environment", return_value={"build_id": BUILD}),
            patch(f"{MODULE}.perf_counter", side_effect=lambda: clock[0]),
            patch(
                f"{MODULE}.psutil.Process",
                side_effect=lambda pid=None: parent if pid is None else worker,
            ),
        ):
            observer = PreparationPerformanceObservation(str(uuid4()), 12, 0.0)
            observer.start(1234, BUILD)
            clock[0] = 1.4
            observer.sample()
            clock[0] = 1.9
            observer.sample()
            clock[0] = 2.0
            observer.sample()
            observer.finish("succeeded", True)
            observer.sample()
        resources = observer.evidence()["resources"]
        self.assertEqual(resources["sample_count"], 2)
        self.assertEqual(resources["sampled_peak_parent_rss_bytes"], 100)
        self.assertEqual(resources["sampled_peak_worker_rss_bytes"], 90)
        self.assertEqual(resources["sampled_peak_combined_rss_bytes"], 170)
        self.assertEqual(resources["sample_interval_ms"], 1000)
        self.assertAlmostEqual(resources["parent_cpu_seconds"], 0.3)
        self.assertEqual(parent.memory_info.call_count, 2)
        self.assertEqual(worker.memory_info.call_count, 2)

    def test_worker_configuration_is_sanitized_and_attempt_sequence_is_contiguous(self):
        records = []
        with (
            patch(f"{MODULE}.execution_environment", return_value={"build_id": BUILD}),
            patch.object(PreparationPerformanceObservation, "sample"),
        ):
            observer = PreparationPerformanceObservation(
                str(uuid4()), 12, 0.0, records.append
            )
            observer.start(1234, BUILD)
            observer.accept_worker_event(
                {
                    "configuration": {
                        "polars_threads_requested": 4,
                        "polars_threads_effective": 4,
                        "source": "PREPARATION_OVERRIDE",
                        "credential": "must disappear",
                    },
                    "workload": {
                        "row_count": 12,
                        "dataset_name": "private customer table",
                    },
                    "environment": {
                        "build_id": BUILD,
                        "absolute_path": "/private/customer",
                    },
                    "arbitrary": "must disappear",
                }
            )
            observer.phase("TRANSFORMING", 50, total_rows=12)
            observer.finish("succeeded", True)
            observer.finish("failed", False)
        self.assertEqual(
            [item["sequence"] for item in records], list(range(len(records)))
        )
        self.assertEqual(records[-1]["event_kind"], "attempt_finished")
        self.assertEqual(records[-1]["configuration"]["polars_threads_effective"], 4)
        self.assertEqual(records[-1]["configuration"]["worker_processes"], 1)
        self.assertNotIn("credential", str(records))
        self.assertNotIn("private customer", str(records))
        self.assertNotIn("/private/customer", str(records))
        snapshot = observer.evidence()
        snapshot["configuration"]["polars_threads_effective"] = 99
        self.assertEqual(
            observer.evidence()["configuration"]["polars_threads_effective"], 4
        )

    def test_inaccessible_process_is_unknown_memory_and_never_blocks_preparation(self):
        records = []
        with (
            patch(f"{MODULE}.execution_environment", return_value={"build_id": BUILD}),
            patch(f"{MODULE}.psutil.Process", side_effect=psutil.AccessDenied(1234)),
        ):
            observer = PreparationPerformanceObservation(
                str(uuid4()), 12, 0.0, records.append
            )
            observer.start(1234, BUILD)
            observer.finish("interrupted", False, failure_reason="WORKER_EXITED")
        resources = observer.evidence()["resources"]
        self.assertEqual(resources["sample_count"], 0)
        self.assertEqual(resources["sampling_failures"], 1)
        self.assertFalse(resources["sampling_complete"])
        self.assertNotIn("sampled_peak_worker_rss_bytes", resources)
        self.assertFalse(records[-1]["measurement_complete"])

    def test_recording_failure_is_isolated_and_marks_evidence_incomplete(self):
        recorder = Mock(side_effect=OSError("diagnostic storage unavailable"))
        with (
            patch(f"{MODULE}.execution_environment", return_value={"build_id": BUILD}),
            patch.object(PreparationPerformanceObservation, "sample"),
        ):
            observer = PreparationPerformanceObservation(
                str(uuid4()), 12, 0.0, recorder
            )
            observer.start(1234, BUILD)
            observer.phase("QUALITY", 25, timing_scope="inclusive")
            observer.phase("quality_evaluation", 20, timing_scope="nested")
            observer.finish("succeeded", True)
        result = observer.evidence()
        self.assertEqual(result["outcome"], "succeeded")
        self.assertFalse(result["measurement_complete"])
        self.assertGreater(result["resources"]["sampling_failures"], 0)

    def test_phase_retention_is_bounded(self):
        with (
            patch(f"{MODULE}.execution_environment", return_value={"build_id": BUILD}),
            patch.object(PreparationPerformanceObservation, "sample"),
        ):
            observer = PreparationPerformanceObservation(str(uuid4()), 12, 0.0)
            observer.start(1234, BUILD)
            for _ in range(MAX_RETAINED_PHASES + 10):
                observer.phase("QUALITY", 1)
            observer.finish("succeeded", True)
        result = observer.evidence()
        self.assertEqual(len(result["phases"]), MAX_RETAINED_PHASES)
        self.assertFalse(result["measurement_complete"])

    def test_environment_capture_uses_metadata_and_omits_machine_identity(self):
        with (
            patch(f"{MODULE}.platform.system", return_value="Windows"),
            patch(f"{MODULE}.platform.release", return_value="11"),
            patch(f"{MODULE}.platform.machine", return_value="AMD64"),
            patch(
                f"{MODULE}.version",
                side_effect=lambda name: {"polars": "1.43.2", "duckdb": "1.4.0"}[name],
            ),
            patch(f"{MODULE}.os.cpu_count", return_value=16),
            patch(f"{MODULE}.psutil.Process") as process,
            patch(
                f"{MODULE}.psutil.virtual_memory",
                return_value=SimpleNamespace(
                    total=32_000_000_000, available=16_000_000_000
                ),
            ),
        ):
            process.return_value.cpu_affinity.return_value = list(range(8))
            environment = execution_environment(BUILD)
        self.assertEqual(environment["operating_system"], "Windows")
        self.assertEqual(environment["logical_cpu_count"], 16)
        self.assertEqual(environment["available_cpu_count"], 8)
        self.assertEqual(environment["polars_version"], "1.43.2")
        self.assertEqual(environment["build_id"], BUILD)
        self.assertNotIn("hostname", environment)
        self.assertNotIn("username", environment)

    def test_environment_probe_failure_returns_partial_facts_without_failing_worker(
        self,
    ):
        with patch(
            f"{MODULE}.platform.system",
            side_effect=RuntimeError("platform unavailable"),
        ):
            self.assertEqual(execution_environment(BUILD), {"build_id": BUILD})


if __name__ == "__main__":
    unittest.main()
