"""Keep captured blanks, missing evidence and privacy states distinct."""

import unittest

from impodo.domain.preflight.fields_set_aside import (
    FieldSetAsideReason,
    preparation_field_reason,
)
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

    def test_saved_scalar_failures_have_an_explicit_field_omission_reason(self):
        for code in (
            "SOURCE_CONCATENATION_INVALID",
            "SOURCE_SELECTION_RULE_INVALID",
            "SOURCE_DECIMAL_ROUNDING_INVALID",
            "SOURCE_PATTERN_INPUT_TOO_LONG",
            "SOURCE_PATTERN_MISMATCH",
            "SOURCE_REPLACEMENT_INVALID",
            "SOURCE_TEXT_LENGTH_INVALID",
            "SOURCE_TEXT_SEGMENT_INVALID",
            "SOURCE_VALUE_RULE_INVALID",
            "REFERENCE_POLICY_MISSING",
            "REFERENCE_INPUT_MISSING",
            "REFERENCE_OUTPUT_MISSING",
        ):
            with self.subTest(code=code):
                self.assertIs(
                    preparation_field_reason(code), FieldSetAsideReason.INVALID
                )
        self.assertIsNone(preparation_field_reason("UNRELATED_RECORD_DIAGNOSTIC"))
