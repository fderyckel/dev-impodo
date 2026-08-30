"""Keep the opt-in completed-load correction qualification bounded."""

from __future__ import annotations

import unittest

from scripts.qualify_completed_load_correction import (
    RELATIONSHIP_CORRECTIONS,
    SCALAR_CORRECTIONS,
    SCALAR_SOURCE_ROWS,
    SCALAR_UNCHANGED,
    _require_disposable_database,
    _review_scope,
    _seed_scope,
    run_vectorized_fixture,
)


class CompletedLoadCorrectionQualificationTests(unittest.TestCase):
    def test_runner_accepts_only_an_explicit_disposable_database(self):
        _require_disposable_database("impodo_correction_20260830")
        with self.assertRaisesRegex(SystemExit, "impodo_correction_"):
            _require_disposable_database("production")

    def test_vectorized_fixture_reduces_only_changed_intent(self):
        candidates, result = run_vectorized_fixture()

        self.assertEqual(len(candidates), SCALAR_CORRECTIONS)
        self.assertEqual(result["source_rows"], SCALAR_SOURCE_ROWS)
        self.assertEqual(result["unchanged_intents"], SCALAR_UNCHANGED)
        self.assertEqual(result["prepared_artifacts"], 2)
        self.assertTrue(all(item.previous is False for item in candidates))
        self.assertTrue(all(item.corrected is True for item in candidates))

    def test_scopes_never_grant_a_unit_write(self):
        review = _review_scope()
        seed = _seed_scope()

        self.assertEqual(review.write_fields("uom.uom"), frozenset())
        self.assertEqual(seed.write_fields("uom.uom"), frozenset())
        self.assertEqual(review.lookup_fields("uom.uom"), frozenset({"name"}))
        self.assertEqual(RELATIONSHIP_CORRECTIONS, 37)


if __name__ == "__main__":
    unittest.main()
