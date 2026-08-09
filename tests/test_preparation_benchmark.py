from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest

from scripts.benchmark_preparation import (
    PreparationBenchmarkError,
    RESULT_PREFIX,
    _require_comparable_results,
    _validate_arguments,
    extract_result,
    summarize,
)


class PreparationBenchmarkHarnessTests(unittest.TestCase):
    def test_extracts_exactly_one_versioned_child_result(self) -> None:
        payload = _result()
        output = "noise\n" + RESULT_PREFIX + json.dumps(payload) + "\nmore noise"

        self.assertEqual(extract_result(output), payload)

        for invalid in (
            "no result",
            RESULT_PREFIX + "{}",
            RESULT_PREFIX + "not-json",
            output + "\n" + RESULT_PREFIX + json.dumps(payload),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PreparationBenchmarkError):
                    extract_result(invalid)

    def test_summary_keeps_individual_runs_and_reports_medians(self) -> None:
        results = (
            _result(wall=3, cpu=2, peak=300, ending=200, database=30),
            _result(wall=1, cpu=1, peak=100, ending=80, database=10),
            _result(wall=2, cpu=1.5, peak=200, ending=120, database=20),
        )

        self.assertEqual(
            summarize(results),
            {
                "median_cpu_seconds": 1.5,
                "median_database_mib": 20.0,
                "median_ending_rss_mib": 120.0,
                "median_peak_working_set_mib": 200.0,
                "median_wall_seconds": 2.0,
                "run_count": 3,
            },
        )

    def test_comparability_requires_identical_fixture_and_runtime(self) -> None:
        first = _result()
        second = _result()
        _require_comparable_results((first, second))

        changed_fixture = _result()
        changed_fixture["fixture"] = {"sha256": "different", "size_bytes": 10}
        with self.assertRaises(PreparationBenchmarkError):
            _require_comparable_results((first, changed_fixture))

        changed_runtime = _result()
        changed_runtime["runtime_versions"] = {"polars": "different"}
        with self.assertRaises(PreparationBenchmarkError):
            _require_comparable_results((first, changed_runtime))

    def test_argument_validation_fails_before_spawning_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            valid = argparse.Namespace(
                runs=3,
                rows=100,
                columns=30,
                mapped_fields=20,
                workload="products",
                dirty=False,
                advanced=False,
                allow_dirty_worktree=False,
                timeout_seconds=60,
                output=Path(temporary) / "result.json",
            )
            _validate_arguments(valid)
            invalid = (
                argparse.Namespace(**{**vars(valid), "runs": 0}),
                argparse.Namespace(**{**vars(valid), "rows": 0}),
                argparse.Namespace(**{**vars(valid), "columns": 2}),
                argparse.Namespace(**{**vars(valid), "mapped_fields": 31}),
                argparse.Namespace(**{**vars(valid), "timeout_seconds": 0}),
                argparse.Namespace(
                    **{
                        **vars(valid),
                        "output": Path(temporary) / "missing" / "result.json",
                    }
                ),
            )
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(PreparationBenchmarkError):
                        _validate_arguments(arguments)


def _result(
    *,
    wall: float = 2,
    cpu: float = 1.5,
    peak: float = 200,
    ending: float = 120,
    database: float = 20,
) -> dict[str, object]:
    return {
        "columns": 30,
        "cpu_seconds": cpu,
        "database_mib": database,
        "dirty": False,
        "ending_rss_mib": ending,
        "fixture": {"sha256": "fixture", "size_bytes": 10},
        "mapped_fields": 20,
        "peak_working_set_mib": peak,
        "revision": "revision",
        "rows": 100,
        "runtime_versions": {"polars": "1.43.2"},
        "schema_version": 1,
        "wall_seconds": wall,
        "workload": "products",
    }


if __name__ == "__main__":
    unittest.main()
