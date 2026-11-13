from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.benchmark_preparation_workers import (
    RESULT_PREFIX,
    PreparationWorkerBenchmarkError,
    _require_comparable_results,
    _require_worktree_unchanged,
    _run_once,
    _thread_runtime,
    _validate_arguments,
    _worktree_fingerprint,
    build_parser,
    extract_result,
    run_fresh_processes,
    summarize,
    summarize_thread_matrix,
)


class PreparationWorkerBenchmarkHarnessTests(unittest.TestCase):
    def test_extracts_one_versioned_result(self) -> None:
        payload = _result()
        output = "noise\n" + RESULT_PREFIX + json.dumps(payload) + "\n"

        self.assertEqual(extract_result(output), payload)
        for invalid in (
            "no result",
            RESULT_PREFIX + "{}",
            output + RESULT_PREFIX + json.dumps(payload),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(PreparationWorkerBenchmarkError):
                extract_result(invalid)

    def test_summarizes_first_repeat_cpu_memory_time_and_storage(self) -> None:
        first = _result(first_peak=300, repeat_peak=250)
        second = _result(first_peak=200, repeat_peak=150)

        summary = summarize((first, second))

        self.assertEqual(summary["median_first_peak_worker_mib"], 250)
        self.assertEqual(summary["maximum_first_peak_worker_mib"], 300)
        self.assertEqual(summary["median_repeat_peak_worker_mib"], 200)
        self.assertEqual(summary["median_first_database_used_mib"], 20)
        self.assertEqual(summary["maximum_parent_repeat_delta_mib"], 3)

    def test_comparability_requires_hashes_exit_and_snapshot_reuse(self) -> None:
        first = _result()
        _require_comparable_results((first, _result()))

        mutations = (
            {"workers_exited": False},
            {"source_reopened": True},
            {"prepared_snapshot_reused": False},
            {"hashes": {"staging": "different"}},
            {"worker_build_ids": {"first": "different", "repeat": "different"}},
            {"memory_measurement": "os_peak_working_set"},
            {
                "vectorization_report": {
                    **dict(first["vectorization_report"]),
                    "python_row_callbacks": 1,
                }
            },
        )
        for mutation in mutations:
            changed = {**_result(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(PreparationWorkerBenchmarkError):
                _require_comparable_results((first, changed))

    def test_rejects_a_worktree_change_during_worker_runs(self) -> None:
        with patch(
            "scripts.benchmark_preparation_workers._worktree_fingerprint",
            return_value="after",
        ), self.assertRaisesRegex(
            PreparationWorkerBenchmarkError,
            "worktree changed",
        ):
            _require_worktree_unchanged("before")

    def test_worktree_fingerprint_includes_git_head(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=b"")
        with (
            patch(
                "scripts.benchmark_preparation_workers._revision",
                side_effect=("before", "before", "after", "after"),
            ),
            patch(
                "scripts.benchmark_preparation_workers.subprocess.run",
                return_value=completed,
            ),
        ):
            before = _worktree_fingerprint()
            after = _worktree_fingerprint()

        self.assertNotEqual(before, after)

    def test_worktree_fingerprint_rejects_head_change_during_capture(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=b"")
        with (
            patch(
                "scripts.benchmark_preparation_workers._revision",
                side_effect=("before", "after"),
            ),
            patch(
                "scripts.benchmark_preparation_workers.subprocess.run",
                return_value=completed,
            ),
            self.assertRaisesRegex(
                PreparationWorkerBenchmarkError,
                "Git HEAD changed",
            ),
        ):
            _worktree_fingerprint()

    def test_thread_runtime_requires_actual_matching_first_and_repeat_evidence(self) -> None:
        result = _result(threads=2)
        self.assertEqual(_thread_runtime(result, expected=2)["first"]["effective_polars_threads"], 2)
        invalid = (
            {**result, "thread_runtime": None},
            {**result, "thread_runtime": {"first": result["thread_runtime"]["first"]}},
            {**result, "thread_runtime": {
                "first": {"requested_polars_threads": 2, "effective_polars_threads": 1},
                "repeat": result["thread_runtime"]["repeat"],
            }},
            _result(threads=4),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(PreparationWorkerBenchmarkError):
                _thread_runtime(candidate, expected=2)

    def test_child_receives_thread_setting_without_mutating_parent_environment(self) -> None:
        arguments = build_parser().parse_args(["--polars-threads", "2"])
        completed = SimpleNamespace(
            returncode=0,
            stdout=RESULT_PREFIX + json.dumps(_result(threads=2)),
            stderr="",
        )
        with (
            patch.dict(os.environ, {"IMPODO_PREPARATION_THREADS": "4"}),
            patch("scripts.benchmark_preparation_workers.subprocess.run", return_value=completed) as run,
        ):
            result = _run_once(arguments, revision="revision", run_number=1)
            self.assertEqual(run.call_args.kwargs["env"]["IMPODO_PREPARATION_THREADS"], "2")
            self.assertEqual(os.environ["IMPODO_PREPARATION_THREADS"], "4")
        self.assertEqual(result["fresh_outer_process_run"], 1)

    def test_matrix_rotates_order_and_reports_each_configuration_separately(self) -> None:
        arguments = build_parser().parse_args(["--thread-matrix"])
        order: list[int] = []

        def run_once(case, **_kwargs):
            order.append(case.polars_threads)
            return _result(threads=case.polars_threads, wall=6 / case.polars_threads)

        with (
            patch("scripts.benchmark_preparation_workers._revision", return_value="revision"),
            patch("scripts.benchmark_preparation_workers._worktree_fingerprint", return_value="fixed"),
            patch("scripts.benchmark_preparation_workers._worktree_dirty", return_value=False),
            patch("scripts.benchmark_preparation_workers._require_worktree_unchanged"),
            patch("scripts.benchmark_preparation_workers._run_once", side_effect=run_once),
        ):
            report = run_fresh_processes(arguments)
        self.assertEqual(order, [1, 2, 4, 2, 4, 1, 4, 1, 2])
        summary = report["summary"]
        self.assertEqual(summary["run_count"], 9)
        self.assertEqual(summary["configurations"]["2"]["first_wall_time_gain_percent"], 50)
        self.assertEqual(summary["configurations"]["4"]["run_count"], 3)

    def test_matrix_rejects_changed_semantics_and_missing_configurations(self) -> None:
        results = (_result(threads=1), _result(threads=2), _result(threads=4))
        summarize_thread_matrix(results)
        changed = {**results[2], "hashes": {"staging": "different"}}
        for invalid in (results[:2], (*results[:2], changed)):
            with self.subTest(invalid=invalid), self.assertRaises(PreparationWorkerBenchmarkError):
                summarize_thread_matrix(invalid)
        with self.assertRaises(PreparationWorkerBenchmarkError):
            _require_comparable_results(results)

    def test_rejects_unsupported_thread_count_before_spawning(self) -> None:
        arguments = build_parser().parse_args([])
        arguments.polars_threads = 3
        with self.assertRaises(PreparationWorkerBenchmarkError):
            _validate_arguments(arguments)


def _result(
    *,
    first_peak: float = 300,
    repeat_peak: float = 250,
    threads: int = 1,
    wall: float = 3,
) -> dict[str, object]:
    def attempt(peak: float) -> dict[str, object]:
        return {
            "cpu_seconds": 2,
            "peak_worker_mib": peak,
            "storage": {
                "database_file_bytes": 30 * 1024 * 1024,
                "database_used_bytes": 20 * 1024 * 1024,
                "project_storage_bytes": 40 * 1024 * 1024,
            },
            "wall_seconds": wall,
        }

    return {
        "columns": 30,
        "dirty": False,
        "effect_fields": 1,
        "first": attempt(first_peak),
        "fixture": {"sha256": "fixture", "size_bytes": 10},
        "hashes": {
            "normalization": "n",
            "quality": "q",
            "staging": "s",
        },
        "mapped_fields": 20,
        "memory_measurement": "sampled_rss",
        "memory_sample_interval_ms": 50,
        "parent_rss": {"repeat_delta_mib": 3},
        "platform": "test-platform",
        "python": "3.12.12",
        "prepared_snapshot_reused": True,
        "repeat": attempt(repeat_peak),
        "revision": "revision",
        "rows": 100,
        "runtime_versions": {"duckdb": "1"},
        "schema_version": 1,
        "source_reopened": False,
        "thread_runtime": {
            name: {"requested_polars_threads": threads, "effective_polars_threads": threads}
            for name in ("first", "repeat")
        },
        "vectorization_report": {
            "bounded_execution_plan_verified": True,
            "full_canonical_rows_constructed": 0,
            "full_prepared_records_constructed": 0,
            "global_operations_classification": "SET_GLOBAL",
            "optimized_plan_verified": True,
            "python_cell_callbacks": 0,
            "python_row_callbacks": 0,
            "row_weighted_native_coverage_percent": 100.0,
            "rule_impact_python_replay_rows": 0,
        },
        "workers_exited": True,
        "worker_build_ids": {"first": "build", "repeat": "build"},
        "workload": "products",
    }


if __name__ == "__main__":
    unittest.main()
