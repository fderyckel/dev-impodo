"""Keep new diagnostic attribution useful, bounded and private after ZIP export."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from uuid import uuid4
from zipfile import ZipFile

from impodo.application.preflight_jobs import PreflightJobManager, PreflightJobResult
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.shared.io_performance import collect_file_io, measure_file_io
from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    LocalDiagnosticRecorder,
    create_diagnostic_bundle,
)
from impodo.web.performance_attribution import (
    sanitize_file_io,
    sanitize_logging_costs,
    sanitize_numeric_validation,
    sanitize_worker_startup,
)
from impodo.web.preparation_route_diagnostics import sanitize_dataset_workload


def route(index: int = 1) -> dict[str, object]:
    """Return fictional opaque route evidence without business identifiers."""

    return {
        "dataset_hash": f"sha256:{index:064x}",
        "row_count": 500,
        "predicted_route": "BOUNDED_PYTHON",
        "actual_route": "BOUNDED_PYTHON",
        "adaptation_route": "PYTHON_RECORDS",
        "reason_codes": ["COLUMNAR_FORMULA_UNSUPPORTED"],
        "outcome": "SUCCEEDED",
        "duration_ms": 25,
    }


class AttributionExportTests(TestCase):
    def test_resanitizes_new_sections_and_keeps_legacy_missing_values_unavailable(self):
        attempt_id = str(uuid4())
        payload = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event_kind": "attempt_finished",
            "operation": "preparation",
            "sequence": 0,
            "outcome": "succeeded",
            "workload": {"dataset_routes": [route()], "private": "DO_NOT_EXPORT"},
            "file_io": {
                "scope": "instrumented_artifact_boundaries",
                "hash_operations": 2,
                "hash_bytes": 400,
                "hash_ms": 1.25,
                "filename": "DO_NOT_EXPORT",
            },
            "numeric_validation": {
                "cells_examined": 1000,
                "findings_generated": 70,
                "max_value_page_bytes": 6000,
                "elapsed_ms": 5.5,
                "value": "DO_NOT_EXPORT",
            },
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / DIAGNOSTIC_LOG_NAME
            path.write_text(
                json.dumps(
                    {
                        "event": "operation_performance",
                        "recorded_at": "2026-11-13T10:00:00Z",
                        "performance": payload,
                    }
                )
                + "\n",
                encoding="utf-8",
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
                preparation = json.loads(archive.read("performance.json"))["attempts"][
                    0
                ]
                common = json.loads(archive.read("performance-summary.json"))
        self.assertNotIn("DO_NOT_EXPORT", contents)
        self.assertEqual(preparation["numeric_validation"]["cells_examined"], 1000)
        self.assertEqual(preparation["file_io"]["hash_bytes"], 400)
        self.assertEqual(common["attempts"][0]["file_io"]["hash_operations"], 2)
        self.assertNotIn("read_bytes", preparation["file_io"])
        self.assertEqual(preparation["section_coverage"]["resources"], "unavailable")
        self.assertEqual(common["logging"]["status"], "unavailable")
        self.assertIn("COLUMNAR_FORMULA_UNSUPPORTED", contents)

    def test_logging_costs_are_correlated_and_exported_without_recursive_events(self):
        identity = str(uuid4())
        with TemporaryDirectory() as directory:
            recorder = LocalDiagnosticRecorder(directory)
            recorder.record_request(
                request_id=identity,
                method="GET",
                route_template="/health",
                status_code=200,
                duration_ms=2,
                file_io={
                    "scope": "instrumented_artifact_boundaries",
                    "read_operations": 1,
                    "read_bytes": 10,
                },
                worker_startup={
                    "scope": "source_worker_parent_intervals",
                    "operations": {
                        "inspection": {
                            "worker_count": 1,
                            "start_call_ms": 2.5,
                            "path": "DO_NOT_EXPORT",
                        }
                    },
                },
            )
            snapshot = recorder.logging_costs_snapshot()
            recorder.close()
            before = (Path(directory) / DIAGNOSTIC_LOG_NAME).read_bytes()
            snapshot["hostname"] = "DO_NOT_EXPORT"
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory,
                        build_contract=PROCESS_BUILD_CONTRACT,
                        logging_costs=snapshot,
                    )
                )
            ) as archive:
                summary = json.loads(archive.read("performance-summary.json"))
                exported = "\n".join(
                    archive.read(name).decode() for name in archive.namelist()
                )
            self.assertEqual(
                before, (Path(directory) / DIAGNOSTIC_LOG_NAME).read_bytes()
            )
        self.assertNotIn("DO_NOT_EXPORT", exported)
        self.assertEqual(summary["logging"]["status"], "available")
        self.assertGreater(summary["logging"]["metrics"]["diagnostic_write_count"], 0)
        self.assertEqual(summary["logging"]["child_process_coverage"], "unavailable")
        self.assertEqual(summary["request_observations"][0]["request_id"], identity)
        self.assertEqual(
            summary["request_observations"][0]["file_io"]["read_bytes"], 10
        )
        request = json.loads(before.splitlines()[0])
        self.assertEqual(
            request["worker_startup"]["operations"]["inspection"]["start_call_ms"], 2.5
        )
        self.assertEqual(request["file_io"]["read_bytes"], 10)

    def test_background_comparison_owns_its_artifact_observation(self):
        events = []
        completed = Event()
        workspace = str(uuid4())
        access = WorkspaceAccessContext(
            project_id=str(uuid4()),
            workspace_id=workspace,
            data_version_id=str(uuid4()),
            migration_run_id=str(uuid4()),
        )

        def record(payload):
            if payload.get("event") == "performance_operation_completed":
                events.append(dict(payload))
                completed.set()

        manager = PreflightJobManager(performance_sink=record)
        self.addCleanup(manager.shutdown)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.bin"
            path.write_bytes(b"fictional artifact")

            def work(_progress):
                with measure_file_io("read") as transfer:
                    content = path.read_bytes()
                    if transfer is not None:
                        transfer.add_bytes(len(content))
                return PreflightJobResult(str(uuid4()), "/summary", "Saved")

            with collect_file_io() as request_files:
                job = manager.enqueue(
                    workspace, "Fictional", access_context=access, work=work
                )
                self.assertTrue(completed.wait(timeout=5))
                self.assertEqual(request_files.snapshot()["status"], "no_observations")
        self.assertEqual(events[0]["operation_id"], job.job_id)
        self.assertEqual(events[0]["file_io"]["read_operations"], 1)
        self.assertEqual(events[0]["file_io"]["read_bytes"], len(b"fictional artifact"))
        self.assertNotIn("fixture.bin", json.dumps(events))

    def test_dataset_routes_deduplicate_rows_and_report_export_omissions(self):
        payload = {
            "dataset_routes": [
                route(),
                route(),
                {**route(2), "dataset_hash": "private"},
            ],
            "dataset_route_coverage": {"dataset_count": 2, "row_count": 1000},
        }
        safe = sanitize_dataset_workload(payload)
        self.assertEqual(len(safe["dataset_routes"]), 1)
        self.assertEqual(
            safe["dataset_route_coverage"],
            {
                "dataset_count": 2,
                "row_count": 1000,
                "captured_dataset_count": 1,
                "captured_row_count": 500,
                "omitted_dataset_count": 1,
                "omitted_row_count": 500,
            },
        )
        many = sanitize_dataset_workload(
            {"dataset_routes": [route(i) for i in range(1, 201)]}
        )
        encoded = json.dumps(many["dataset_routes"], separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), 24 * 1024)
        self.assertLessEqual(len(many["dataset_routes"]), 128)

    def test_optional_numeric_sections_reject_invalid_types_and_values(self):
        safe = sanitize_numeric_validation(
            {
                "cells_examined": True,
                "findings_generated": -1,
                "elapsed_ms": float("nan"),
                "max_value_page_bytes": 42,
            }
        )
        self.assertEqual(safe, {"max_value_page_bytes": 42})
        self.assertEqual(
            sanitize_file_io({"scope": "DO_NOT_EXPORT", "read_bytes": 0}), {}
        )
        self.assertEqual(sanitize_worker_startup({"scope": "DO_NOT_EXPORT"}), {})
        self.assertEqual(
            sanitize_logging_costs({"schema_version": 2}), {"status": "unavailable"}
        )
        self.assertEqual(sanitize_numeric_validation({"status": []}), {})
        self.assertEqual(
            sanitize_file_io(
                {"scope": "instrumented_artifact_boundaries", "status": {}}
            ),
            {"scope": "instrumented_artifact_boundaries"},
        )
        malformed = sanitize_logging_costs(
            {
                "schema_version": 1,
                "scope": "process_recorder",
                "correlations": [{"kind": {}, "id": str(uuid4())}],
            }
        )
        self.assertNotIn("correlations", malformed)
        self.assertEqual(
            sanitize_logging_costs(
                {"schema_version": True, "scope": "process_recorder"}
            ),
            {"status": "unavailable"},
        )
