"""Retain source-read costs in Windows diagnostic exports without source values."""

import json
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.shared.performance import OperationPerformance
from impodo.web.diagnostics import LocalDiagnosticRecorder, create_diagnostic_bundle


class SourceReadDiagnosticExportTests(TestCase):
    def test_windows_bundle_retains_local_costs_alongside_odoo_and_workload_costs(self):
        with (
            TemporaryDirectory() as directory,
            patch(
                "impodo.web.performance_diagnostics.platform.system",
                return_value="Windows",
            ),
            patch(
                "impodo.web.performance_diagnostics.platform.release",
                return_value="11",
            ),
            patch(
                "impodo.web.performance_diagnostics.platform.machine",
                return_value="AMD64",
            ),
        ):
            recorder = LocalDiagnosticRecorder(directory)
            collector = OperationPerformance(
                operation_id=str(uuid4()),
                operation="preflight",
                sink=recorder.record_performance,
                queue_wait_ms=50,
            )
            with collector.phase("verify"):
                with collector.phase("source_package"):
                    collector.count("source_package_reads", 4)
                collector.count("prepared_rows", 6_012)
                collector.metrics(
                    {
                        "database_connection_count": 8,
                        "database_schema_check_count": 6,
                        "database_lock_retry_count": 1,
                        "database_connection_ms": 75,
                        "database_schema_ms": 25,
                        "database_lock_wait_ms": 10,
                        "private customer": 100,
                    }
                )
            with collector.phase("read_odoo"):
                collector.count("rpc_calls", 13)
                collector.count("rpc_rows", 20)
            collector.finish(outcome="succeeded")
            recorder.close()
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(
                        directory,
                        build_contract=PROCESS_BUILD_CONTRACT,
                    )
                )
            ) as archive:
                summary = json.loads(archive.read("performance-summary.json"))
                manifest = json.loads(archive.read("manifest.json"))
                events = archive.read("diagnostics.jsonl").decode()
        attempt = summary["attempts"][0]
        self.assertEqual(attempt["runtime"]["operating_system"], "Windows")
        self.assertEqual(attempt["runtime"]["architecture"], "amd64")
        self.assertEqual(
            attempt["application_build_id"], PROCESS_BUILD_CONTRACT.application_build_id
        )
        self.assertTrue(attempt["terminal_observed"])
        self.assertEqual(attempt["queue_wait_ms"], 50)
        self.assertEqual(attempt["counters"]["source_package_reads"], 4)
        self.assertEqual(attempt["counters"]["prepared_rows"], 6_012)
        self.assertEqual(attempt["counters"]["database_connection_count"], 8)
        self.assertEqual(attempt["counters"]["database_schema_check_count"], 6)
        self.assertEqual(attempt["counters"]["database_lock_retry_count"], 1)
        self.assertEqual(attempt["metrics"]["database_lock_wait_ms"], 10)
        self.assertEqual(attempt["counters"]["rpc_calls"], 13)
        self.assertIn("source_package", attempt["coverage"]["phases"])
        self.assertGreaterEqual(attempt["phases"]["source_package"]["thread_cpu_ms"], 0)
        self.assertEqual(manifest["runtime"]["operating_system"], "Windows")
        self.assertNotIn("private customer", events)
        self.assertIn("overlapping", summary["phase_wall_time"])
        self.assertIn("unavailable", summary["browser_timing"])
