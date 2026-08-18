from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.qualify_transformation_scale import (
    CUSTOMER_BASELINE_REVISION,
    TransformationQualificationError,
    _customer_baseline_evidence,
    _relationship_gates,
    _require_worktree_unchanged,
    _worktree_fingerprint,
    _worker_command,
    _worker_gates,
    scenarios,
)


class TransformationScaleQualificationHarnessTests(unittest.TestCase):
    def test_release_matrix_keeps_business_and_stress_fixtures(self) -> None:
        by_name = {item.name: item for item in scenarios("release")}

        self.assertEqual(by_name["direct_products_100k"].rows, 100_000)
        self.assertEqual(by_name["wide_customer_twin_1k"].peak_limit_mib, 500)
        related = by_name["related_product_bom_96k"]
        self.assertEqual((related.products, related.bom_lines), (16_000, 80_000))
        self.assertEqual(by_name["effect_heavy_4k"].effect_fields, 19)
        self.assertTrue(by_name["dirty_high_effect_4k"].dirty)

    def test_worker_gates_use_maxima_for_first_and_repeat(self) -> None:
        scenario = scenarios("release")[0]
        report = {
            "summary": {
                "maximum_first_peak_worker_mib": 700,
                "maximum_first_cpu_seconds": 90,
                "maximum_first_wall_seconds": 100,
                "maximum_parent_repeat_delta_mib": 20,
                "maximum_repeat_peak_worker_mib": 751,
                "maximum_repeat_cpu_seconds": 95,
                "maximum_repeat_wall_seconds": 110,
                "run_count": 3,
            }
        }

        gates = {
            item["name"]: item
            for item in _worker_gates(
                scenario,
                report,
                expected_runs=3,
            )
        }

        self.assertTrue(gates[f"{scenario.name}.first_peak_worker_mib"]["passed"])
        self.assertFalse(gates[f"{scenario.name}.repeat_peak_worker_mib"]["passed"])
        self.assertFalse(
            gates[f"{scenario.name}.vectorization.evidence_present"]["passed"]
        )

    def test_high_volume_release_requires_complete_vectorization_evidence(
        self,
    ) -> None:
        scenario = scenarios("release")[0]
        report = {
            "summary": {
                "maximum_first_peak_worker_mib": 700,
                "maximum_first_cpu_seconds": 90,
                "maximum_first_wall_seconds": 100,
                "maximum_parent_repeat_delta_mib": 20,
                "maximum_repeat_peak_worker_mib": 700,
                "maximum_repeat_cpu_seconds": 95,
                "maximum_repeat_wall_seconds": 110,
                "run_count": 3,
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
        }

        gates = {
            item["name"]: item
            for item in _worker_gates(
                scenario,
                report,
                expected_runs=3,
            )
        }

        vectorization = [
            item
            for name, item in gates.items()
            if name.startswith(f"{scenario.name}.vectorization.")
        ]
        self.assertTrue(vectorization)
        self.assertTrue(all(item["passed"] for item in vectorization))

    def test_relationship_gates_inspect_each_set_based_run(self) -> None:
        report = {
            "runs": [
                {
                    "set_based_hybrid": {
                        "peak_rss_mib": 400,
                        "wall_seconds": 30,
                    }
                },
                {
                    "set_based_hybrid": {
                        "peak_rss_mib": 901,
                        "wall_seconds": 31,
                    }
                },
            ]
        }

        gates = {
            item["name"]: item for item in _relationship_gates(report, expected_runs=2)
        }

        self.assertTrue(gates["relationship_semantic_parity.wall_seconds"]["passed"])
        self.assertFalse(gates["relationship_semantic_parity.peak_rss_mib"]["passed"])

    def test_worker_and_relationship_gates_reject_missing_runs(self) -> None:
        scenario = scenarios("release")[0]
        worker_report = {
            "summary": {
                "maximum_first_cpu_seconds": 1,
                "maximum_first_peak_worker_mib": 1,
                "maximum_first_wall_seconds": 1,
                "maximum_parent_repeat_delta_mib": 1,
                "maximum_repeat_cpu_seconds": 1,
                "maximum_repeat_peak_worker_mib": 1,
                "maximum_repeat_wall_seconds": 1,
                "run_count": 2,
            }
        }

        worker_gates = _worker_gates(
            scenario,
            worker_report,
            expected_runs=3,
        )
        relationship_gates = _relationship_gates(
            {
                "runs": [
                    {
                        "set_based_hybrid": {
                            "peak_rss_mib": 1,
                            "wall_seconds": 1,
                        }
                    }
                ]
            },
            expected_runs=3,
        )

        self.assertFalse(worker_gates[0]["passed"])
        self.assertFalse(relationship_gates[0]["passed"])

    def test_related_worker_command_is_cross_platform_argument_list(self) -> None:
        scenario = next(
            item for item in scenarios("smoke") if item.workload == "product-bom"
        )
        command = _worker_command(
            scenario,
            runs=1,
            evidence_path=Path("evidence.json"),
            timeout_seconds=300,
            allow_dirty=True,
        )

        self.assertIn("--products", command)
        self.assertIn("--bom-lines", command)
        self.assertIn("--allow-dirty-worktree", command)

    def test_rejects_a_worktree_change_between_scenarios(self) -> None:
        with patch(
            "scripts.qualify_transformation_scale._worktree_fingerprint",
            return_value="after",
        ):
            with self.assertRaisesRegex(
                TransformationQualificationError,
                "worktree changed",
            ):
                _require_worktree_unchanged("before")

    def test_worktree_fingerprint_includes_git_head(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=b"")
        with (
            patch(
                "scripts.qualify_transformation_scale._revision",
                side_effect=("before", "before", "after", "after"),
            ),
            patch(
                "scripts.qualify_transformation_scale.subprocess.run",
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
                "scripts.qualify_transformation_scale._revision",
                side_effect=("before", "after"),
            ),
            patch(
                "scripts.qualify_transformation_scale.subprocess.run",
                return_value=completed,
            ),
            self.assertRaisesRegex(
                TransformationQualificationError,
                "Git HEAD changed",
            ),
        ):
            _worktree_fingerprint()

    def test_release_requires_same_runtime_customer_gain_baseline(self) -> None:
        candidate = _customer_candidate()
        missing = _customer_baseline_evidence(None, candidate)

        self.assertFalse(missing["gates"][0]["passed"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(
                json.dumps(_customer_baseline()),
                encoding="utf-8",
            )
            matching = _customer_baseline_evidence(path, candidate)

            self.assertTrue(all(item["passed"] for item in matching["gates"]))
            self.assertAlmostEqual(
                matching["evidence"]["gain_percent"],
                30.0,
            )

            changed = _customer_baseline()
            changed["runs"][0]["runtime_versions"] = {"duckdb": "different"}
            path.write_text(json.dumps(changed), encoding="utf-8")
            mismatched = _customer_baseline_evidence(path, candidate)

            gates = {item["name"]: item for item in mismatched["gates"]}
            self.assertFalse(gates["customer_baseline_same_platform_runtime"]["passed"])


def _customer_candidate() -> dict[str, object]:
    return {
        "runs": [
            {
                "fixture": {"sha256": "fixture", "size_bytes": 2_000_000},
                "platform": "Windows-reference",
                "python": "3.12",
                "runtime_versions": {"duckdb": "1.5.5"},
            }
        ],
        "summary": {
            "maximum_first_peak_worker_mib": 200,
            "maximum_repeat_peak_worker_mib": 210,
        },
    }


def _customer_baseline() -> dict[str, object]:
    run = {
        "fixture": {"sha256": "fixture", "size_bytes": 2_000_000},
        "platform": "Windows-reference",
        "python": "3.12",
        "runtime_versions": {"duckdb": "1.5.5"},
    }
    return {
        "command": {
            "advanced": False,
            "columns": 150,
            "dirty": False,
            "mapped_fields": 20,
            "rows": 1_000,
            "runs": 3,
            "workload": "customers",
        },
        "revision": CUSTOMER_BASELINE_REVISION,
        "runs": [run, dict(run), dict(run)],
        "summary": {
            "median_peak_process_tree_mib": 300,
            "run_count": 3,
        },
        "worktree_dirty": False,
    }


if __name__ == "__main__":
    unittest.main()
