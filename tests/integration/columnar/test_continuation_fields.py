"""Verify added and removed fields use canonical prepared values."""

from dataclasses import replace
from unittest import TestCase

from impodo.adapters.polars_correction import (
    iter_polars_correction_candidate_batches,
    write_polars_correction_candidates,
)
from impodo.domain.correction import CorrectionFieldOutcome, classify_correction_field
from tests.integration.columnar import test_polars_correction as fixtures


class ContinuationFieldTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.PolarsCorrectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def _candidates(self, previous, revised):
        before, old_path, old_snapshot = self.fixture._prepare(
            previous, "before.parquet"
        )
        after, new_path, new_snapshot = self.fixture._prepare(revised, "after.parquet")
        artifact = write_polars_correction_candidates(
            old_path,
            old_snapshot,
            before,
            new_path,
            new_snapshot,
            after,
            self.fixture.root / "candidates.parquet",
            allow_field_changes=True,
            audit_unchanged=True,
        )
        return tuple(
            candidate
            for batch in iter_polars_correction_candidate_batches(artifact)
            for candidate in batch
        )

    def test_added_field_has_no_previous_intent_and_removed_field_is_untouched(self):
        revised = self.fixture.previous_definition
        dataset = revised.datasets[0]
        old = replace(
            revised,
            datasets=(
                replace(
                    dataset,
                    fields=tuple(
                        field
                        for field in dataset.fields
                        if field.target_field != "name"
                    ),
                ),
            ),
        )
        candidates = self._candidates(old, revised)
        added = next(item for item in candidates if item.target_field == "name")
        self.assertFalse(added.previous_present)
        self.assertIsNone(added.previous)
        self.assertEqual(
            classify_correction_field(added, "Later Odoo value").outcome,
            CorrectionFieldOutcome.READY,
        )

    def test_removed_mapping_does_not_become_a_clear(self):
        previous = self.fixture.previous_definition
        dataset = previous.datasets[0]
        revised = replace(
            previous,
            datasets=(
                replace(
                    dataset,
                    fields=tuple(
                        field
                        for field in dataset.fields
                        if field.target_field != "name"
                    ),
                ),
            ),
        )
        candidates = self._candidates(previous, revised)
        self.assertNotIn("name", {item.target_field for item in candidates})

    def test_independent_odoo_change_is_preserved_when_intent_is_unchanged(self):
        candidates = self._candidates(
            self.fixture.previous_definition, self.fixture.previous_definition
        )
        candidate = next(item for item in candidates if item.target_field == "name")
        self.assertEqual(
            classify_correction_field(candidate, "Changed directly in Odoo").outcome,
            CorrectionFieldOutcome.PRESERVED_IN_ODOO,
        )
