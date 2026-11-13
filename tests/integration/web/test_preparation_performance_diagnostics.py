"""Value-free Stage 4 evidence survives export without inventing coverage."""

from __future__ import annotations

import json
import math
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    LocalDiagnosticRecorder,
    create_diagnostic_bundle,
)
from impodo.web.performance_bundle import performance_bundle_members
from impodo.web.preparation_performance_diagnostics import (
    PERFORMANCE_EVENT,
    sanitize_performance_event,
)


def event(
    kind: str = "attempt_started", *, sequence: int = 0, attempt_id: str | None = None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "attempt_id": attempt_id or str(uuid4()),
        "operation": "preparation",
        "event_kind": kind,
        "sequence": sequence,
    }


def record(payload: dict[str, object]) -> dict[str, object]:
    return {
        "event": PERFORMANCE_EVENT,
        "recorded_at": "2026-11-13T09:00:00Z",
        "performance": payload,
    }


class PreparationPerformanceSanitizerTests(unittest.TestCase):
    def test_preserves_every_current_route_and_inherited_configuration(self) -> None:
        from impodo.application.workspace.preparation.preparation_capability import (
            PreparationRouteBehavior,
        )

        for route in PreparationRouteBehavior:
            with self.subTest(route=route.value):
                safe = sanitize_performance_event({
                    **event(),
                    "configuration": {"source": "inherited_polars"},
                    "workload": {
                        "processing_route": route.value,
                        "stage_routes": {"transformation": route.value},
                    },
                })
                self.assertIsNotNone(safe)
                assert safe is not None
                self.assertEqual(safe["configuration"], {"source": "INHERITED_POLARS"})
                self.assertEqual(safe["workload"], {
                    "processing_route": route.value,
                    "stage_routes": {"transformation": route.value},
                })

    def test_keeps_only_execution_metadata_counts_and_closed_configuration(
        self,
    ) -> None:
        payload = {
            **event(),
            "private_value": "NEVER_EXPORT_THIS",
            "environment": {
                "operating_system": "Windows",
                "os_version": "10.0.26100",
                "architecture": "AMD64",
                "python_version": "3.12.14",
                "polars_version": "1.43.2",
                "duckdb_version": "1.4.1",
                "build_id": "sha256:" + "a" * 64,
                "available_cpu_count": 8,
                "total_memory_bytes": 32 * 1024**3,
                "hostname": "NEVER_EXPORT_THIS",
                "absolute_path": "NEVER_EXPORT_THIS",
            },
            "configuration": {
                "polars_threads_requested": 4,
                "polars_threads_effective": 4,
                "worker_processes": 1,
                "source": "preparation_override",
                "duckdb_configuration": "UNCHANGED",
                "api_key": "NEVER_EXPORT_THIS",
            },
            "workload": {
                "row_count": 50_000,
                "column_count": 30,
                "processing_route": "MIXED_BOUNDED",
                "stage_routes": {
                    "quality": "BOUNDED_DURABLE",
                    "NEVER_EXPORT_THIS": "NATIVE_COLUMNAR",
                },
                "filename": "NEVER_EXPORT_THIS",
                "values": ["NEVER_EXPORT_THIS"],
            },
        }
        safe = sanitize_performance_event(payload)
        self.assertIsNotNone(safe)
        self.assertNotIn("NEVER_EXPORT_THIS", json.dumps(safe))
        self.assertEqual(safe["environment"]["operating_system"], "Windows")
        self.assertEqual(safe["environment"]["os_version"], "10.0.26100")
        self.assertEqual(safe["configuration"]["source"], "PREPARATION_OVERRIDE")
        self.assertEqual(
            safe["workload"]["stage_routes"], {"quality": "BOUNDED_DURABLE"}
        )

    def test_rejects_invalid_identity_phase_and_nonfinite_or_false_metrics(
        self,
    ) -> None:
        for changes in (
            {"schema_version": True},
            {"schema_version": 2},
            {"attempt_id": "source-row-identity"},
            {"operation": "NEVER_EXPORT_THIS"},
            {"event_kind": "unexpected"},
            {
                "event_kind": "phase_completed",
                "phase": "NEVER_EXPORT_THIS",
                "duration_ms": 1,
            },
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(sanitize_performance_event({**event(), **changes}))
        safe = sanitize_performance_event(
            {
                **event(),
                "resources": {
                    "cpu_seconds": math.nan,
                    "parent_cpu_seconds": math.inf,
                    "sample_count": True,
                    "sample_interval_ms": -1,
                    "sampling_duration_ms": 10**1000,
                },
                "workload": {"row_count": "10", "column_count": False},
            }
        )
        self.assertEqual(safe["resources"], {})
        self.assertEqual(safe["workload"], {})

    def test_recorder_failure_is_optional_and_invalid_capture_is_visible(self) -> None:
        with TemporaryDirectory(prefix="impodo-performance-") as directory:
            recorder = LocalDiagnosticRecorder(directory)
            recorder.record_performance_event({"schema_version": 2})
            self.assertEqual(recorder.capture_failure_count, 1)
            with patch.object(recorder, "_write", side_effect=OSError("private path")):
                recorder.record_performance_event(event())
            self.assertEqual(recorder.capture_failure_count, 2)
            recorder.close()


class PreparationPerformanceBundleTests(unittest.TestCase):
    def test_exports_execution_time_windows_facts_and_detects_missing_phases(
        self,
    ) -> None:
        attempt_id = str(uuid4())
        start = {
            **event(attempt_id=attempt_id),
            "environment": {"operating_system": "Windows", "python_version": "3.12.14"},
            "configuration": {"polars_threads_requested": 2, "source": "DEFAULT"},
        }
        phase = {
            **event("phase_completed", sequence=2, attempt_id=attempt_id),
            "phase": "quality_evaluation",
            "duration_ms": 123.5,
            "timing_scope": "nested",
        }
        finish = {
            **event("attempt_finished", sequence=3, attempt_id=attempt_id),
            "outcome": "succeeded",
            "measurement_complete": True,
            "duration_ms": 500,
            "configuration": {"polars_threads_effective": 2},
            "resources": {
                "cpu_seconds": 0.6,
                "sample_count": 1,
                "sampled_peak_worker_rss_bytes": 128 * 1024**2,
            },
        }
        with TemporaryDirectory(prefix="impodo-performance-") as directory:
            recorder = LocalDiagnosticRecorder(directory)
            for payload in (start, phase, finish):
                recorder.record_performance_event(payload)
            recorder.close()
            with patch(
                "impodo.web.performance_diagnostics.platform.system",
                return_value="Darwin",
            ):
                data = create_diagnostic_bundle(
                    directory, build_contract=PROCESS_BUILD_CONTRACT
                )
            with ZipFile(BytesIO(data)) as archive:
                performance = json.loads(archive.read("performance.json"))
                summary = archive.read("performance-summary.txt").decode()
        attempt = performance["attempts"][0]
        self.assertEqual(attempt["environment"]["operating_system"], "Windows")
        self.assertEqual(attempt["configuration"]["polars_threads_effective"], 2)
        self.assertFalse(attempt["coverage"]["retained_history_complete"])
        self.assertEqual(attempt["coverage"]["sequence_status"], "partial")
        self.assertTrue(attempt["producer_measurement_complete"])
        self.assertIn("123.5 ms (nested)", summary)
        self.assertIn("not an exact peak", summary)
        self.assertIn("partial or unavailable", summary)

    def test_general_record_window_does_not_erase_retained_performance_attempt(
        self,
    ) -> None:
        attempt_id = str(uuid4())
        with TemporaryDirectory(prefix="impodo-performance-") as directory:
            recorder = LocalDiagnosticRecorder(directory)
            recorder.record_performance_event(event(attempt_id=attempt_id))
            recorder.record_performance_event(
                {
                    **event("attempt_finished", sequence=1, attempt_id=attempt_id),
                    "outcome": "succeeded",
                    "measurement_complete": True,
                    "duration_ms": 1,
                }
            )
            for _ in range(5):
                recorder.record_lifecycle("ordinary_request")
            recorder.close()
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory,
                        build_contract=PROCESS_BUILD_CONTRACT,
                        max_records=1,
                    )
                )
            ) as archive:
                performance = json.loads(archive.read("performance.json"))
                general = archive.read("diagnostics.jsonl").decode().splitlines()
        self.assertEqual(len(general), 1)
        self.assertEqual(performance["attempts"][0]["attempt_id"], attempt_id)
        self.assertTrue(
            performance["attempts"][0]["coverage"]["retained_history_complete"]
        )
        self.assertGreater(performance["coverage"]["general_omitted_record_count"], 0)

    def test_resanitizes_tampered_logs_and_reports_old_or_crashed_attempts(
        self,
    ) -> None:
        payload = {
            **event("attempt_finished"),
            "outcome": "crashed",
            "measurement_complete": False,
            "environment": {"operating_system": "NEVER_EXPORT_THIS"},
            "resources": {"cpu_seconds": "NEVER_EXPORT_THIS"},
            "configuration": {"source": "NEVER_EXPORT_THIS"},
        }
        with TemporaryDirectory(prefix="impodo-performance-") as directory:
            path = Path(directory) / DIAGNOSTIC_LOG_NAME
            path.write_text(
                json.dumps({**record(payload), "path": "NEVER_EXPORT_THIS"}) + "\n"
            )
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory, build_contract=PROCESS_BUILD_CONTRACT
                    )
                )
            ) as archive:
                contents = "\n".join(
                    archive.read(name).decode() for name in archive.namelist()
                )
                performance = json.loads(archive.read("performance.json"))
        self.assertNotIn("NEVER_EXPORT_THIS", contents)
        attempt = performance["attempts"][0]
        self.assertEqual(attempt["outcome"], "crashed")
        self.assertFalse(attempt["coverage"]["start_present"])
        self.assertEqual(attempt["section_coverage"]["resources"], "unavailable")
        self.assertNotIn("cpu_seconds", attempt["resources"])
        members, coverage = performance_bundle_members([])
        self.assertEqual(coverage["status"], "unavailable")
        self.assertIn("evidence unavailable", members["performance-summary.txt"])

    def test_bounds_attempts_and_events_with_explicit_omissions(self) -> None:
        many = [record(event()) for _ in range(21)]
        members, coverage = performance_bundle_members(many)
        self.assertEqual(len(json.loads(members["performance.json"])["attempts"]), 20)
        self.assertEqual(coverage["omitted_attempt_count"], 1)
        self.assertEqual(coverage["omitted_event_count"], 1)
        attempt_id = str(uuid4())
        events = [record(event(attempt_id=attempt_id))]
        events.extend(
            record(
                {
                    **event("phase_completed", sequence=i, attempt_id=attempt_id),
                    "phase": "QUALITY",
                    "duration_ms": 1,
                }
            )
            for i in range(1, 1025)
        )
        members, coverage = performance_bundle_members(events)
        attempt = json.loads(members["performance.json"])["attempts"][0]
        self.assertEqual(coverage["retained_event_count"], 1024)
        self.assertEqual(coverage["omitted_event_count"], 1)
        self.assertFalse(attempt["coverage"]["start_present"])


if __name__ == "__main__":
    unittest.main()
