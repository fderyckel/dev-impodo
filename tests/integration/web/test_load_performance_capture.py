"""Verify load cost attribution, bounded retention, and runtime privacy."""

from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

import duckdb

from impodo.adapters.duckdb.request_timing import (
    collect_duckdb_request_timings,
    current_duckdb_request_timings,
    record_duckdb_connection_timing,
    record_duckdb_schema_timing,
)
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.execution.load_observation import (
    LoadObservation,
    bind_load_observation,
    emit_load_evidence,
    observe_load_local_phase,
    observe_load_stage,
)
from impodo.web.diagnostics import LocalDiagnosticRecorder, create_diagnostic_bundle
from impodo.web.load_diagnostics import sanitize_load_record
from impodo.web.load_performance import sanitize_load_costs
from impodo.web.runtime_diagnostics import (
    _static_runtime_metadata,
    runtime_performance_metadata,
    sanitize_runtime_metadata,
)


def _bundle(directory):
    with ZipFile(
        BytesIO(
            create_diagnostic_bundle(
                directory,
                build_contract=PROCESS_BUILD_CONTRACT,
            )
        )
    ) as archive:
        return {
            name: archive.read(name).decode()
            for name in ("manifest.json", "load-attempts.json", "load-events.jsonl")
        }


class LoadPerformanceCaptureTests(unittest.TestCase):
    def test_runtime_and_phase_costs_are_bound_to_execution_and_survive_export(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            observer = recorder.load_observation(action="LOAD", request_id=str(uuid4()))
            with (
                bind_load_observation(observer),
                collect_duckdb_request_timings() as request_costs,
            ):
                record_duckdb_connection_timing(
                    duration_ms=25, lock_wait_ms=0, lock_retry_count=0
                )
                emit_load_evidence("worker_started")
                with observe_load_stage("write"):
                    with observe_load_local_phase("journal_before_call"):
                        with DuckDbConnectionFactory().connect(
                            Path(directory) / "synthetic.duckdb"
                        ) as connection:
                            self.assertEqual(
                                connection.execute("SELECT 42").fetchone(), (42,)
                            )
                        record_duckdb_schema_timing(duration_ms=3)
                    self.assertIs(current_duckdb_request_timings(), request_costs)
                emit_load_evidence("terminal", outcome="SUCCEEDED")
                self.assertEqual(request_costs.connection_count, 2)
            recorder.close()
            members = _bundle(directory)
        events = [
            json.loads(line) for line in members["load-events.jsonl"].splitlines()
        ]
        worker = next(event for event in events if event["event"] == "worker_started")
        self.assertEqual(worker["runtime"]["duckdb_version"], duckdb.__version__)
        self.assertIn("physical_cpu_count", worker["runtime"])
        self.assertIn("unavailable_fields", worker["runtime"]["coverage"])
        stage = next(event for event in events if event["event"] == "stage_finished")
        self.assertEqual(stage["performance"]["database_connection_count"], 1)
        self.assertEqual(stage["performance"]["database_schema_check_count"], 1)
        self.assertEqual(stage["performance"]["cpu_scope"], "stage_context_thread_only")
        attempt = json.loads(members["load-attempts.json"])[0]
        self.assertEqual(attempt["runtime"], worker["runtime"])
        self.assertEqual(
            attempt["local_phase_metrics"]["journal_before_call"]["calls"], 1
        )
        self.assertEqual(
            attempt["local_phase_metrics"]["journal_before_call"]["cpu_scope"],
            "owning_local_thread",
        )
        self.assertEqual(
            attempt["stage_performance"]["write"]["database_connection_count"], 1
        )
        self.assertEqual(
            attempt["performance_coverage"]["security_storage_power_attribution"],
            "unavailable",
        )

    def test_row_phases_aggregate_without_row_logging_or_memory_sampling(self):
        with (
            TemporaryDirectory() as directory,
            patch("impodo.web.load_diagnostics.MAX_ATTEMPT_EVENTS", 3),
        ):
            recorder = LocalDiagnosticRecorder(directory)
            observer = recorder.load_observation(action="LOAD", request_id=str(uuid4()))
            with patch("impodo.web.load_performance.psutil.Process") as process:
                process.return_value.memory_info.return_value.rss = 1024
                with bind_load_observation(observer), observe_load_stage("write"):
                    for _ in range(2_200):
                        with observe_load_local_phase("journal_after_call"):
                            record_duckdb_connection_timing(
                                duration_ms=1, lock_wait_ms=0, lock_retry_count=0
                            )
                self.assertEqual(process.call_count, 1)
            with bind_load_observation(observer):
                emit_load_evidence("terminal", outcome="SUCCEEDED")
            recorder.close()
            members = _bundle(directory)
        events = [
            json.loads(line) for line in members["load-events.jsonl"].splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "performance_checkpoint" for event in events), 16
        )
        self.assertLessEqual(len(events), 20)
        attempt = json.loads(members["load-attempts.json"])[0]
        costs = attempt["local_phase_metrics"]["journal_after_call"]
        self.assertEqual(costs["calls"], 2_200)
        self.assertEqual(costs["database_connection_count"], 2_200)
        self.assertNotIn("process_rss_bytes", costs)
        self.assertEqual(
            attempt["stage_performance"]["write"]["database_connection_count"], 2_200
        )
        self.assertGreater(attempt["omitted_event_count"], 0)

    def test_interrupted_attempt_exports_its_last_bounded_checkpoint(self):
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            observer = recorder.load_observation(action="LOAD", request_id=str(uuid4()))
            with bind_load_observation(observer):
                for _ in range(128):
                    with observe_load_local_phase("progress_publication"):
                        pass
            recorder.close()
            attempt = json.loads(_bundle(directory)["load-attempts.json"])[0]
        self.assertTrue(attempt["terminal_missing"])
        self.assertEqual(
            attempt["local_phase_metrics"]["progress_publication"]["calls"], 128
        )
        self.assertFalse(attempt["performance_coverage"]["terminal_aggregate_present"])

    def test_optional_provider_entry_and_exit_failures_preserve_body_and_exception(
        self,
    ):
        @contextmanager
        def entry_failure(_sample_memory):
            raise OSError("PRIVATE_ENTER")
            yield {}  # pragma: no cover - contextmanager requires a generator

        @contextmanager
        def exit_failure(_sample_memory):
            yield {}
            raise OSError("PRIVATE_EXIT")

        for provider in (entry_failure, exit_failure):
            with self.subTest(provider=provider.__name__):
                records = []
                observer = LoadObservation(
                    str(uuid4()),
                    lambda event, facts: records.append((event, facts)),
                    stage_metrics=provider,
                )
                expected = RuntimeError("BUSINESS_EXCEPTION")
                with bind_load_observation(observer):
                    with (
                        self.assertRaises(RuntimeError) as caught,
                        observe_load_stage("write"),
                        observe_load_local_phase("journal_after_call"),
                    ):
                        raise expected
                self.assertIs(caught.exception, expected)
                self.assertEqual(
                    observer.local_phases["journal_after_call"]["calls"], 1
                )
                self.assertEqual(
                    observer.local_phases["journal_after_call"][
                        "capture_failure_count"
                    ],
                    1,
                )
                self.assertEqual(records[-1][1]["outcome"], "FAILED")
                self.assertNotIn("PRIVATE_", str(records))

    def test_invalid_provider_number_never_changes_business_result_or_exception(self):
        @contextmanager
        def invalid_metrics(_sample_memory):
            yield {"database_connection_count": 10**1000}

        observer = LoadObservation(
            str(uuid4()), lambda *_args: None, stage_metrics=invalid_metrics
        )
        expected = RuntimeError("ORIGINAL")
        with bind_load_observation(observer):
            with observe_load_local_phase("journal_after_call"):
                result = 42
            with (
                self.assertRaises(RuntimeError) as caught,
                observe_load_local_phase("journal_after_call"),
            ):
                raise expected
        self.assertEqual(result, 42)
        self.assertIs(caught.exception, expected)
        self.assertEqual(observer.local_phases["journal_after_call"]["calls"], 2)
        self.assertEqual(
            observer.local_phases["journal_after_call"]["capture_failure_count"], 2
        )

    def test_cost_and_runtime_resanitization_rejects_private_and_invalid_values(self):
        runtime = sanitize_runtime_metadata(
            {
                "operating_system": "Windows",
                "hostname": "PRIVATE_HOST",
                "operating_system_build": "PRIVATE_PATH",
                "duckdb_version": "1.5.5",
                "logical_cpu_count": True,
                "physical_cpu_count": float("nan"),
                "total_memory_bytes": -1,
                "available_memory_bytes": 0,
                "process_bitness": 64,
                "process_architecture": "amd64",
                "native_architecture": "arm64",
                "architecture_source": "windows_api",
            }
        )
        self.assertEqual(runtime["emulation_status"], "emulated")
        self.assertIsNone(runtime["logical_cpu_count"])
        self.assertIsNone(runtime["physical_cpu_count"])
        self.assertEqual(runtime["available_memory_bytes"], 0)
        self.assertEqual(runtime["memory_scope"], "system_wide")
        self.assertNotIn("PRIVATE", str(runtime))
        costs = sanitize_load_costs(
            {
                "database_connection_count": True,
                "db_connect_ms": float("nan"),
                "thread_cpu_ms": float("inf"),
                "process_rss_bytes": -1,
                "customer": "PRIVATE_VALUE",
            }
        )
        self.assertEqual(costs["coverage"]["database"], "unavailable")
        self.assertNotIn("PRIVATE", str(costs))
        record = sanitize_load_record(
            {
                "event": "terminal",
                "attempt_id": str(uuid4()),
                "recorded_at": "2026-11-13T12:00:00Z",
                "runtime": runtime,
                "stage_performance": {"PRIVATE_STAGE": {"wall_ms": 99}},
            }
        )
        self.assertEqual(record["stage_performance"], {})
        self.assertNotIn("PRIVATE", str(record))

    def test_optional_runtime_probes_fail_independently_and_bundle_still_exports(self):
        _static_runtime_metadata.cache_clear()
        try:
            with (
                patch(
                    "impodo.web.runtime_diagnostics.platform.system",
                    side_effect=OSError,
                ),
                patch(
                    "impodo.web.runtime_diagnostics.psutil.cpu_count",
                    side_effect=OSError,
                ),
                patch(
                    "impodo.web.runtime_diagnostics.psutil.virtual_memory",
                    side_effect=OSError,
                ),
            ):
                runtime = runtime_performance_metadata()
                self.assertEqual(runtime["operating_system"], "Unknown")
                self.assertEqual(runtime["duckdb_version"], duckdb.__version__)
                self.assertIsNone(runtime["logical_cpu_count"])
                self.assertIsNone(runtime["physical_cpu_count"])
                self.assertIsNone(runtime["total_memory_bytes"])
                with TemporaryDirectory() as directory:
                    recorder = LocalDiagnosticRecorder(directory)
                    with patch(
                        "impodo.web.load_diagnostics.runtime_performance_metadata",
                        side_effect=OSError,
                    ):
                        observer = recorder.load_observation(
                            action="LOAD", request_id=str(uuid4())
                        )
                        with bind_load_observation(observer):
                            emit_load_evidence("worker_started")
                            emit_load_evidence("terminal", outcome="SUCCEEDED")
                    recorder.close()
                    attempt = json.loads(_bundle(directory)["load-attempts.json"])[0]
                self.assertEqual(attempt["outcome"], "SUCCEEDED")
                self.assertIsNone(attempt["runtime"]["duckdb_version"])
                self.assertIn(
                    "duckdb_version",
                    attempt["runtime"]["coverage"]["unavailable_fields"],
                )
        finally:
            _static_runtime_metadata.cache_clear()


if __name__ == "__main__":
    unittest.main()
