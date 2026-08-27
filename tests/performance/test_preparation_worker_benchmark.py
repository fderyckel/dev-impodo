from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.benchmark_preparation_workers import (
    PreparationWorkerBenchmarkError,
    RESULT_PREFIX,
    _require_comparable_results,
    _require_worktree_unchanged,
    _worktree_fingerprint,
    extract_result,
    summarize,
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
            with self.subTest(invalid=invalid):
                with self.assertRaises(PreparationWorkerBenchmarkError):
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
            {
                "vectorization_report": {
                    **dict(first["vectorization_report"]),
                    "python_row_callbacks": 1,
                }
            },
        )
        for mutation in mutations:
            changed = {**_result(), **mutation}
            with self.subTest(mutation=mutation):
                with self.assertRaises(PreparationWorkerBenchmarkError):
                    _require_comparable_results((first, changed))

    def test_rejects_a_worktree_change_during_worker_runs(self) -> None:
        with patch(
            "scripts.benchmark_preparation_workers._worktree_fingerprint",
            return_value="after",
        ):
            with self.assertRaisesRegex(
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


def _result(
    *,
    first_peak: float = 300,
    repeat_peak: float = 250,
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
            "wall_seconds": 3,
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
        "parent_rss": {"repeat_delta_mib": 3},
        "platform": "test-platform",
        "prepared_snapshot_reused": True,
        "repeat": attempt(repeat_peak),
        "revision": "revision",
        "rows": 100,
        "runtime_versions": {"duckdb": "1"},
        "schema_version": 1,
        "source_reopened": False,
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
        "workload": "products",
    }


if __name__ == "__main__":
    unittest.main()
