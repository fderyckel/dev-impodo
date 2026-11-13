"""Verify the portable fictional Stage 5 read benchmark's evidence contract."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from impodo.application.shared.build_contract import (
    PROCESS_BUILD_CONTRACT,
    ApplicationBuildMismatchError,
)
from scripts.benchmark_preflight_reads import (
    FictionalTransport,
    LocalDiagnosticRecorder,
    ReadFixture,
    run_benchmark,
    summarize_reads,
)


class PreflightReadBenchmarkTests(unittest.TestCase):
    def test_diagnostics_cohorts_capture_real_logs_and_preserve_results(self) -> None:
        with patch(
            "scripts.benchmark_preflight_reads.LocalDiagnosticRecorder",
            wraps=LocalDiagnosticRecorder,
        ) as recorder:
            report = run_benchmark(
                ReadFixture(4, 5, 2, 0), samples=2, diagnostics_mode="both"
            )
        runs = report["raw_runs"]
        self.assertEqual(len(runs), 32)
        self.assertEqual(len(report["summaries"]), 16)
        self.assertEqual(recorder.call_count, 16)
        self.assertTrue(
            all(not Path(call.args[0]).exists() for call in recorder.call_args_list)
        )
        self.assertEqual(
            {summary["diagnostics_mode"] for summary in report["summaries"]},
            {"off", "on"},
        )
        for run in runs:
            with self.subTest(
                major=run["major"],
                workers=run["workers"],
                diagnostics=run["diagnostics_mode"],
            ):
                if run["diagnostics_mode"] == "off":
                    self.assertEqual(run["diagnostic_record_count"], 0)
                    self.assertEqual(run["diagnostic_log_bytes"], 0)
                    continue
                self.assertGreaterEqual(run["diagnostic_record_count"], 6)
                self.assertGreater(run["diagnostic_log_bytes"], 0)
                self.assertEqual(run["diagnostic_counters"]["rpc_batches"], 4)
                self.assertEqual(run["diagnostic_counters"]["rpc_rows"], 20)
                self.assertEqual(
                    run["diagnostic_counters"]["rpc_pages"], run["record_page_calls"]
                )
                self.assertEqual(
                    run["diagnostic_counters"]["rpc_calls"],
                    run["record_page_calls"] + run["version_calls"],
                )
                self.assertLessEqual(run["diagnostic_observed_workers"], run["workers"])
        for major in (19, 20):
            self.assertEqual(
                len({run["semantic_hash"] for run in runs if run["major"] == major}), 1
            )
        cold_order = [
            (run["diagnostics_mode"], run["workers"])
            for run in runs
            if run["major"] == 19 and run["temperature"] == "cold_connector"
        ]
        self.assertEqual(
            cold_order,
            [
                ("off", 1),
                ("on", 1),
                ("off", 2),
                ("on", 2),
                ("on", 2),
                ("off", 2),
                ("on", 1),
                ("off", 1),
            ],
        )

    def test_diagnostics_off_does_not_initialize_a_recorder(self) -> None:
        with patch(
            "scripts.benchmark_preflight_reads.LocalDiagnosticRecorder"
        ) as recorder:
            report = run_benchmark(
                ReadFixture(2, 1, 2, 0), samples=1, workers=(1,), majors=(20,)
            )
        recorder.assert_not_called()
        self.assertTrue(
            all(run["diagnostics_mode"] == "off" for run in report["raw_runs"])
        )

    def test_changed_source_cannot_produce_comparable_measurements(self) -> None:
        changed = replace(
            PROCESS_BUILD_CONTRACT, application_build_id="sha256:" + "a" * 64
        )
        for observations in ((changed,), (PROCESS_BUILD_CONTRACT, changed)):
            with (
                self.subTest(observations=observations),
                patch(
                    "scripts.benchmark_preflight_reads.calculate_application_build_contract",
                    side_effect=observations,
                ),
                self.assertRaises(ApplicationBuildMismatchError),
            ):
                run_benchmark(
                    ReadFixture(2, 1, 2, 0), samples=1, workers=(1,), majors=(20,)
                )

    def test_paired_reads_preserve_results_pages_and_cold_repeat_coverage(self) -> None:
        report = run_benchmark(ReadFixture(4, 5, 2, 0), samples=2)
        runs = report["raw_runs"]

        self.assertEqual(len(runs), 16)
        self.assertEqual(len(report["summaries"]), 8)
        for run in runs:
            with self.subTest(
                major=run["major"],
                workers=run["workers"],
                sample=run["sample"],
                temperature=run["temperature"],
            ):
                self.assertEqual(run["rows"], 20)
                self.assertEqual(run["record_page_calls"], 12)
                self.assertEqual(
                    run["version_calls"],
                    1 if run["temperature"] == "cold_connector" else 0,
                )
                self.assertLessEqual(run["peak_in_flight_reads"], run["workers"])
                self.assertGreater(run["process_peak_observed_rss_bytes"], 0)
                self.assertGreaterEqual(run["wall_seconds"], 0)
        for major in (19, 20):
            self.assertEqual(
                len({run["semantic_hash"] for run in runs if run["major"] == major}), 1
            )
        self.assertNotEqual(runs[0]["semantic_hash"], runs[-1]["semantic_hash"])

        first_cold = [
            run["workers"]
            for run in runs
            if run["major"] == 19 and run["temperature"] == "cold_connector"
        ]
        self.assertEqual(first_cold, [1, 2, 2, 1])

    def test_exact_page_multiple_requires_the_completeness_page(self) -> None:
        report = run_benchmark(ReadFixture(2, 4, 2, 0), samples=1, workers=(1,))
        self.assertTrue(
            all(run["record_page_calls"] == 6 for run in report["raw_runs"])
        )

    def test_summary_rejects_different_results_and_query_counts(self) -> None:
        report = run_benchmark(ReadFixture(2, 1, 2, 0), samples=1, majors=(20,))
        runs = report["raw_runs"]
        for name, changed in (
            ("semantic_hash", "sha256:changed"),
            ("record_page_calls", 999),
            ("rows", 999),
        ):
            with self.subTest(name=name):
                altered = [
                    {**run, **({name: changed} if index == 0 else {})}
                    for index, run in enumerate(runs)
                ]
                with self.assertRaisesRegex(AssertionError, name):
                    summarize_reads(altered)

    def test_incomplete_transport_evidence_is_not_a_completed_measurement(self) -> None:
        transport = FictionalTransport(ReadFixture(2, 5, 2, 0), 20)
        transport.offsets = {("x.fixture_component", 0): [0, 2, 4]}
        with self.assertRaisesRegex(AssertionError, "omitted a batch"):
            transport.assert_complete(2)
        transport.offsets[("x.fixture_resource", 1)] = [0, 4, 2]
        with self.assertRaisesRegex(AssertionError, "reordered a page"):
            transport.assert_complete(2)
        transport.offsets[("x.fixture_resource", 1)] = [0, 2, 4]
        transport.peak_in_flight = 3
        with self.assertRaisesRegex(AssertionError, "concurrency bound"):
            transport.assert_complete(2)

    def test_invalid_settings_fail_before_any_network_fixture_runs(self) -> None:
        for values in (
            (0, 5, 2, 0),
            (2, 0, 2, 0),
            (2, 5, 0, 0),
            (2, 5, 2, -1),
            (2, 5, 2, float("nan")),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ReadFixture(*values)
        for options in (
            {"samples": 0},
            {"workers": ()},
            {"workers": (0,)},
            {"workers": (True,)},
            {"workers": (1, 1)},
            {"majors": (18,)},
            {"majors": (19, 19)},
            {"diagnostics_mode": "unknown"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run_benchmark(ReadFixture(2, 1, 2, 0), **options)


if __name__ == "__main__":
    unittest.main()
