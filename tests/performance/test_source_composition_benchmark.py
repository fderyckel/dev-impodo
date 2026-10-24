"""Regression gates for deterministic exact-join qualification evidence."""

from __future__ import annotations

import unittest

from scripts.benchmark_source_composition import _validate, build_fixture


def _request_evidence(*, connections: int, schema_checks: int) -> dict[str, object]:
    return {
        "database": {
            "connection_count": connections,
            "schema_check_count": schema_checks,
        }
    }


def _run_evidence(rows: int) -> dict[str, object]:
    fixture = build_fixture(rows, 2).evidence()
    return {
        "fixture": fixture,
        "semantic_review": {
            "actual_output_rows": rows,
            "findings": [],
        },
        "odoo_calls": {
            "connection": 0,
            "schema": 0,
            "model_catalog": 0,
            "read_identity": 0,
        },
        "preview": _request_evidence(connections=6, schema_checks=10),
        "save": _request_evidence(connections=6, schema_checks=6),
    }


class SourceCompositionBenchmarkTests(unittest.TestCase):
    def test_fixture_bytes_and_width_evidence_are_deterministic(self) -> None:
        first = build_fixture(25, 2)
        second = build_fixture(25, 2)
        wide = build_fixture(25, 22)

        self.assertEqual(first, second)
        self.assertEqual(first.evidence()["left_sha256"], second.evidence()["left_sha256"])
        self.assertNotEqual(first.evidence()["left_sha256"], wide.evidence()["left_sha256"])
        self.assertEqual(first.evidence()["columns_per_input"], 4)
        self.assertEqual(wide.evidence()["columns_per_input"], 24)
        self.assertEqual(wide.evidence()["projected_output_columns"], 44)

    def test_parity_and_database_lifetime_are_hard_gates(self) -> None:
        run = _run_evidence(25)
        cases = {"narrow": {"runs": [run, _run_evidence(25)]}}

        gates = _validate(cases, 25)

        self.assertEqual(gates["exact_result_parity_across_fresh_processes"], "passed")
        self.assertEqual(gates["database_lifetime_ceilings"], "passed")
        self.assertFalse(gates["supported_limit_changed"])

    def test_connection_regression_is_rejected_without_timing_thresholds(self) -> None:
        run = _run_evidence(25)
        run["preview"] = _request_evidence(connections=13, schema_checks=10)

        with self.assertRaisesRegex(RuntimeError, "connection ownership"):
            _validate({"narrow": {"runs": [run]}}, 25)


if __name__ == "__main__":
    unittest.main()
