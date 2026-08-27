from __future__ import annotations

import unittest

from impodo.domain.preflight.reports import (
    ReviewWorkbookActionPriority,
    ReviewWorkbookCellEffect,
    ReviewWorkbookCellStatus,
    review_workbook_action_priority,
    review_workbook_cell_feedback,
)


class ReviewWorkbookCellFeedbackTests(unittest.TestCase):
    def _effect(self, *, before: str, after: str) -> ReviewWorkbookCellEffect:
        return ReviewWorkbookCellEffect(
            source_trace_id="sha256:" + "a" * 64,
            dataset="contacts",
            source_row=2,
            target_field="name",
            before=before,
            after=after,
            rule_name="Trim surrounding spaces",
            explanation="Impodo removed spaces around this value.",
        )

    def test_frozen_effect_distinguishes_added_and_changed_values(self) -> None:
        added = review_workbook_cell_feedback(
            "Added name",
            (self._effect(before="—", after="Added name"),),
        )
        changed = review_workbook_cell_feedback(
            "Prepared name",
            (self._effect(before=" Raw name ", after="Prepared name"),),
        )

        self.assertEqual(added.status, ReviewWorkbookCellStatus.ADDED)
        self.assertEqual(changed.status, ReviewWorkbookCellStatus.CHANGED)
        self.assertEqual(changed.original_value, " Raw name ")
        self.assertIn("Trim surrounding spaces", changed.note)

    def test_manifest_issue_takes_precedence_over_preparation_effect(self) -> None:
        feedback = review_workbook_cell_feedback(
            "Prepared name",
            (self._effect(before="Raw name", after="Prepared name"),),
            issue_severity="error",
            issue_message="A required value is missing.",
        )

        self.assertEqual(feedback.status, ReviewWorkbookCellStatus.NEEDS_ATTENTION)
        self.assertEqual(feedback.note, "A required value is missing.")

    def test_blank_without_manifest_issue_is_informational(self) -> None:
        feedback = review_workbook_cell_feedback(None, required=True)

        self.assertEqual(feedback.status, ReviewWorkbookCellStatus.EMPTY_ALLOWED)
        self.assertIn("found no missing-value blocker", feedback.note)

    def test_nonblank_value_without_effect_is_as_provided(self) -> None:
        feedback = review_workbook_cell_feedback("Example contact")

        self.assertEqual(feedback.status, ReviewWorkbookCellStatus.AS_PROVIDED)


class ReviewWorkbookActionPriorityTests(unittest.TestCase):
    def test_errors_must_be_fixed_and_warnings_require_review(self) -> None:
        self.assertEqual(
            review_workbook_action_priority("error"),
            ReviewWorkbookActionPriority.MUST_FIX,
        )
        self.assertEqual(
            review_workbook_action_priority("WARNING"),
            ReviewWorkbookActionPriority.REVIEW,
        )

    def test_unknown_manifest_severity_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be error or warning"):
            review_workbook_action_priority("informational")


if __name__ == "__main__":
    unittest.main()
