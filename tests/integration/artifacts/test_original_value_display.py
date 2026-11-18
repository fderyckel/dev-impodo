"""Render original snapshot evidence as literal text with explicit privacy states."""

import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import (
    ReportGenerationError,
    write_review_workbook,
)
from impodo.domain.preflight.fields_set_aside import FieldSetAside, FieldSetAsideReason
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalSourceInput,
    OriginalSourceValue,
)
from impodo.domain.source_snapshot import EncodedSourceCell
from tests.integration.artifacts.test_fields_set_aside_coverage import (
    _empty_manifest,
    _evidence,
    _fields,
)


def _coordinate(row, key):
    return OriginalSourceCoordinate("dataset:partners", "partners", row, key)


class OriginalValueDisplayTests(unittest.TestCase):
    def test_literal_text_blanks_and_unavailable_for_both_target_majors_and_modes(self):
        values = ("00123", "  x  ", "=1+2", "31/02/2026", None, "")
        findings = tuple(
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                row,
                ("value",),
                "name",
                "The source value was rejected.",
                (
                    OriginalSourceInput(
                        _coordinate(row, "value"),
                        OriginalSourceValue.from_cell(
                            EncodedSourceCell.from_python(value)
                        ),
                    ),
                ),
            )
            for row, value in enumerate(values, 2)
        ) + (
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                8,
                ("value",),
                "name",
                "No exact source evidence.",
            ),
        )
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    directory = Path(temporary)
                    manifest = _empty_manifest(directory, major, mode)
                    path = write_review_workbook(
                        manifest,
                        directory / "review.xlsx",
                        review_evidence=_evidence(_fields(findings)),
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        originals = tuple(sheet.cell(row, 5) for row in range(7, 14))
                        self.assertEqual(
                            tuple(cell.value for cell in originals[:4]), values[:4]
                        )
                        self.assertTrue(
                            all(cell.data_type == "s" for cell in originals[:4])
                        )
                        self.assertIsNone(originals[4].value)
                        self.assertIsNone(originals[5].value)
                        self.assertIn("blank cell", originals[4].comment.text)
                        self.assertIn("empty text", originals[5].comment.text)
                        self.assertEqual(originals[6].value, "Unavailable")
                        self.assertIn(
                            "1 finding(s) have no available original value",
                            sheet["A2"].value,
                        )

    def test_restricted_values_are_withheld_even_if_supplied(self):
        finding = FieldSetAside(
            FieldSetAsideReason.INVALID,
            "partners",
            2,
            ("name",),
            "name",
            "Invalid source.",
            (
                OriginalSourceInput(
                    _coordinate(2, "name"),
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("secret-value")
                    ),
                ),
            ),
        )
        fields = replace(_fields((finding,)), source_values_restricted=True)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _empty_manifest(directory)
            path = write_review_workbook(
                manifest,
                directory / "review.xlsx",
                preview_directory=directory / "preview",
                review_evidence=_evidence(fields),
            )
            with closing(load_workbook(path)) as workbook:
                sheet = workbook["Fields set aside"]
                self.assertEqual(sheet["E7"].value, "Withheld")
                self.assertIn("withheld for restricted data", sheet["A2"].value)
                self.assertNotIn("secret-value", str(tuple(sheet.values)))
            self.assertNotIn(
                "secret-value",
                (directory / "preview" / "fields-set-aside.csv").read_text(),
            )

    def test_originals_that_excel_cannot_preserve_fail_with_recovery_advice(self):
        for value, reason in (("x" * 32_768, "text limit"), ("a\x00b", "characters")):
            finding = FieldSetAside(
                FieldSetAsideReason.INVALID,
                "partners",
                2,
                ("name",),
                "name",
                "Invalid source.",
                (
                    OriginalSourceInput(
                        _coordinate(2, "name"),
                        OriginalSourceValue.from_cell(
                            EncodedSourceCell.from_python(value)
                        ),
                    ),
                ),
            )
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                manifest = _empty_manifest(directory)
                with self.assertRaisesRegex(ReportGenerationError, reason):
                    write_review_workbook(
                        manifest,
                        directory / "review.xlsx",
                        review_evidence=_evidence(_fields((finding,))),
                    )
                self.assertFalse((directory / "review.xlsx").exists())

    def test_expanded_inputs_respect_the_excel_row_limit_before_publication(self):
        inputs = tuple(
            OriginalSourceInput(
                _coordinate(2, key), OriginalSourceValue.from_cell(EncodedSourceCell.from_python(value))
            )
            for key, value in (("code", "001"), ("company", "002"))
        )
        findings = tuple(
            FieldSetAside(
                FieldSetAsideReason.UNRECOGNIZED,
                "partners",
                2,
                ("code", "company"),
                target,
                "The related record was not found.",
                inputs,
            )
            for target in ("parent_id", "other_id")
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _empty_manifest(directory)
            path = directory / "too-many-rows.xlsx"
            with (
                patch("impodo.adapters.artifacts.reporting._EXCEL_MAX_ROW", 9),
                self.assertRaisesRegex(ReportGenerationError, "supported rows.*Fields set aside"),
            ):
                write_review_workbook(
                    manifest, path, review_evidence=_evidence(_fields(findings))
                )
            self.assertFalse(path.exists())
