"""Keep captured blanks, missing evidence and privacy states distinct."""

import unittest

from impodo.domain.preflight.original_values import (
    OriginalSourceValue,
    OriginalValueState,
)
from impodo.domain.source_snapshot import EncodedSourceCell


class OriginalValueTests(unittest.TestCase):
    def test_exact_source_scalars_and_blank_kinds(self):
        for raw in ("00123", "  x  ", "=1+2", "31/02/2026", None, ""):
            with self.subTest(raw=raw):
                cell = EncodedSourceCell.from_python(raw)
                original = OriginalSourceValue.from_cell(cell)
                self.assertEqual(original.cell, cell)
                self.assertEqual(original.text, raw)
                self.assertEqual(
                    original.state,
                    OriginalValueState.BLANK
                    if raw in (None, "")
                    else OriginalValueState.PRESENT,
                )
        self.assertNotEqual(
            OriginalSourceValue.from_cell(EncodedSourceCell.from_python(None)),
            OriginalSourceValue.from_cell(EncodedSourceCell.from_python("")),
        )

    def test_unavailable_and_withheld_cannot_carry_source_values(self):
        cell = EncodedSourceCell.from_python("private")
        for state in (OriginalValueState.UNAVAILABLE, OriginalValueState.WITHHELD):
            with self.subTest(state=state), self.assertRaises(ValueError):
                OriginalSourceValue(state, cell)
        self.assertIsNone(OriginalSourceValue.unavailable().text)
        self.assertIsNone(OriginalSourceValue.withheld().text)

    def test_present_and_blank_require_matching_evidence(self):
        for state, cell in (
            (OriginalValueState.PRESENT, None),
            (OriginalValueState.BLANK, None),
            (OriginalValueState.PRESENT, EncodedSourceCell.from_python("")),
            (OriginalValueState.BLANK, EncodedSourceCell.from_python("text")),
        ):
            with self.subTest(state=state, cell=cell), self.assertRaises(ValueError):
                OriginalSourceValue(state, cell)
