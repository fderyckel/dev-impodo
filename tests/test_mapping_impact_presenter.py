from __future__ import annotations

import unittest

from impodo.domain.staging.transformation_impact import TransformationImpactRow
from impodo.web.presenters.mapping_impact import _transformation_impact_row_views


class TransformationImpactPresenterTests(unittest.TestCase):
    def test_edge_spaces_are_explained_when_values_look_identical(self) -> None:
        row = TransformationImpactRow(
            dataset="contacts",
            source_row=9,
            source_column="Telephone",
            target_field="phone",
            raw_value=" +43 000 000 0008 ",
            proposed_value="+43 000 000 0008",
            rules="Source + Trim + Empty to null",
            outcome="changed",
        )

        view = _transformation_impact_row_views((row,))[0]

        self.assertEqual(
            view.original_spacing_note,
            "Contains 1 space before the value and 1 space after the value.",
        )
        self.assertEqual(
            view.prepared_spacing_note,
            "Removed 1 space before the value and 1 space after the value.",
        )

    def test_non_spacing_change_has_no_spacing_note(self) -> None:
        row = TransformationImpactRow(
            dataset="contacts",
            source_row=10,
            source_column="Telephone",
            target_field="phone",
            raw_value="067-77-37-67",
            proposed_value="067773767",
            rules="Remove separators between digits",
            outcome="changed",
        )

        view = _transformation_impact_row_views((row,))[0]

        self.assertEqual(view.original_spacing_note, "")
        self.assertEqual(view.prepared_spacing_note, "")


if __name__ == "__main__":
    unittest.main()
