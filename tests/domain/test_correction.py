"""Verify output-based correction meaning across mapping mistake types."""

from __future__ import annotations

import unittest

from impodo.domain.correction import (
    CorrectionCandidate,
    CorrectionFieldOutcome,
    CorrectionValueKind,
    classify_correction_field,
)


class CorrectionMeaningTests(unittest.TestCase):
    """Keep the three-way rule independent of the edited mapping control."""

    def candidate(
        self,
        previous,
        corrected,
        *,
        field: str = "active",
        kind: CorrectionValueKind = CorrectionValueKind.SCALAR,
    ) -> CorrectionCandidate:
        return CorrectionCandidate(
            dataset="Products",
            source_row=7,
            target_model="product.template",
            target_field=field,
            value_kind=kind,
            previous=previous,
            corrected=corrected,
        )

    def test_ready_when_odoo_still_has_previous_intent(self) -> None:
        decision = classify_correction_field(
            self.candidate(False, True),
            False,
        )

        self.assertEqual(decision.outcome, CorrectionFieldOutcome.READY)
        self.assertTrue(decision.writable)

    def test_already_corrected_is_not_written(self) -> None:
        decision = classify_correction_field(
            self.candidate("draft", "sale", field="state"),
            "sale",
        )

        self.assertEqual(
            decision.outcome,
            CorrectionFieldOutcome.ALREADY_CORRECTED,
        )
        self.assertFalse(decision.writable)

    def test_independent_change_is_a_conflict(self) -> None:
        decision = classify_correction_field(
            self.candidate("pce", "PCE", field="default_code"),
            "Piece",
        )

        self.assertEqual(decision.outcome, CorrectionFieldOutcome.CONFLICT)

    def test_unchanged_output_never_becomes_a_write(self) -> None:
        decision = classify_correction_field(
            self.candidate("Kg", "Kg", field="name"),
            "KG",
        )

        self.assertEqual(
            decision.outcome,
            CorrectionFieldOutcome.UNCHANGED_INTENT,
        )

    def test_many2one_uses_the_same_truth_table(self) -> None:
        decision = classify_correction_field(
            self.candidate(
                ("UNI",),
                ("Unit",),
                field="uom_id",
                kind=CorrectionValueKind.MANY2ONE,
            ),
            ("UNI",),
        )

        self.assertEqual(decision.outcome, CorrectionFieldOutcome.READY)

    def test_canonical_comparator_can_handle_governed_equivalence(self) -> None:
        decision = classify_correction_field(
            self.candidate("kg", "KG", field="name"),
            "Kg",
            equal=lambda left, right: str(left).casefold() == str(right).casefold(),
        )

        self.assertEqual(
            decision.outcome,
            CorrectionFieldOutcome.UNCHANGED_INTENT,
        )


if __name__ == "__main__":
    unittest.main()
