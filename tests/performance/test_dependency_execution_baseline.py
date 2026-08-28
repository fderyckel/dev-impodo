from __future__ import annotations

from copy import deepcopy
import unittest

from scripts.benchmark_dependency_execution import (
    DependencyBenchmarkError,
    _require_same_semantics,
    build_fixture,
    measure_shape,
    summarize,
)
from scripts.benchmark_relationships import _project, _run_child


class DependencyExecutionBaselineTests(unittest.TestCase):
    def test_accepted_fixtures_freeze_rows_edges_and_business_order(self) -> None:
        expectations = {
            "product_unit": (
                14,
                12,
                ("products", "uoms"),
                ("uoms", "products"),
            ),
            "same_dataset_hierarchy": (
                8,
                7,
                ("categories",),
                ("categories",),
            ),
            "optional_cycle": (
                2,
                2,
                ("first_nodes", "second_nodes"),
                ("first_nodes", "second_nodes"),
            ),
            "product_bom": (
                627,
                1125,
                ("bom_lines", "boms", "products", "uoms"),
                ("uoms", "products", "boms", "bom_lines"),
            ),
        }

        for shape, expected in expectations.items():
            with self.subTest(shape=shape):
                fixture = build_fixture(shape)

                self.assertEqual(len(fixture.snapshot.rows), expected[0])
                self.assertEqual(fixture.relationship_edge_count, expected[1])
                self.assertEqual(fixture.reviewed_dataset_order, expected[2])
                self.assertEqual(fixture.expected_dataset_order, expected[3])

    def test_current_execution_call_baseline_is_explicit(self) -> None:
        expected_calls = {
            "product_unit": {
                "create": 0,
                "load_create": 3,
                "lookup": 0,
                "relationship_patch": 0,
                "total": 3,
                "update": 0,
            },
            "same_dataset_hierarchy": {
                "create": 0,
                "load_create": 8,
                "lookup": 0,
                "relationship_patch": 0,
                "total": 8,
                "update": 0,
            },
            "optional_cycle": {
                "create": 0,
                "load_create": 2,
                "lookup": 0,
                "relationship_patch": 1,
                "total": 3,
                "update": 1,
            },
            "product_bom": {
                "create": 0,
                "load_create": 64,
                "lookup": 0,
                "relationship_patch": 0,
                "total": 64,
                "update": 0,
            },
        }

        for shape, calls in expected_calls.items():
            with self.subTest(shape=shape):
                result = measure_shape(shape, batch_size=10)

                self.assertEqual(result["run_status"], "COMPLETED")
                self.assertEqual(result["committed_rows"], result["row_count"])
                self.assertEqual(result["call_counts"], calls)
                self.assertEqual(
                    result["observed_dataset_order"],
                    result["expected_dataset_order"],
                )

    def test_repeated_runs_require_identical_semantic_evidence(self) -> None:
        result = measure_shape("optional_cycle", batch_size=10)
        repeated = deepcopy(result)

        _require_same_semantics("optional_cycle", (result, repeated))
        summary = summarize((result, repeated))
        self.assertEqual(summary["run_count"], 2)
        self.assertEqual(summary["call_counts"]["relationship_patch"], 1)

        repeated["call_sequence_hash"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(DependencyBenchmarkError, "changed semantics"):
            _require_same_semantics("optional_cycle", (result, repeated))

    def test_preparation_relationship_benchmark_uses_current_workspace_contract(
        self,
    ) -> None:
        workspace = _project(4, 12)

        self.assertTrue(workspace.workspace_id)
        control = _run_child(
            route="materialized-control",
            products=4,
            bom_lines=12,
            batch_size=4,
        )
        hybrid = _run_child(
            route="set-based-hybrid",
            products=4,
            bom_lines=12,
            batch_size=4,
        )
        self.assertEqual(control["semantic_summary"], hybrid["semantic_summary"])
        self.assertEqual(control["staging_content_hash"], hybrid["staging_content_hash"])
        self.assertEqual(hybrid["relationship_state_counts"], {"RESOLVED": 12})


if __name__ == "__main__":
    unittest.main()
