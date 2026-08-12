from __future__ import annotations

import json
import unittest

from scripts.benchmark_preparation_workers import (
    PreparationWorkerBenchmarkError,
    RESULT_PREFIX,
    _require_comparable_results,
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
        )
        for mutation in mutations:
            changed = {**_result(), **mutation}
            with self.subTest(mutation=mutation):
                with self.assertRaises(PreparationWorkerBenchmarkError):
                    _require_comparable_results((first, changed))


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
        "workers_exited": True,
        "workload": "products",
    }


if __name__ == "__main__":
    unittest.main()
