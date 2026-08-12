from __future__ import annotations

from pathlib import Path
import unittest

from scripts.qualify_transformation_scale import (
    _relationship_gates,
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
                "maximum_first_wall_seconds": 100,
                "maximum_parent_repeat_delta_mib": 20,
                "maximum_repeat_peak_worker_mib": 751,
                "maximum_repeat_wall_seconds": 110,
            }
        }

        gates = {item["name"]: item for item in _worker_gates(scenario, report)}

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

        gates = {item["name"]: item for item in _relationship_gates(report)}

        self.assertTrue(gates["relationship_semantic_parity.wall_seconds"]["passed"])
        self.assertFalse(gates["relationship_semantic_parity.peak_rss_mib"]["passed"])

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


if __name__ == "__main__":
    unittest.main()
