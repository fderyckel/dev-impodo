from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.qualify_transformation_scale import (
    TransformationQualificationError,
    _relationship_gates,
    _require_worktree_unchanged,
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
        self.assertFalse(
            gates[f"{scenario.name}.repeat_peak_worker_mib"]["passed"]
        )

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
            item["name"]: item
            for item in _relationship_gates(report, expected_runs=2)
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
            item
            for item in scenarios("smoke")
            if item.workload == "product-bom"
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


if __name__ == "__main__":
    unittest.main()
