"""Show reviewed relationship names rather than normalized source link keys."""

import tempfile
import unittest
from dataclasses import replace

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import write_preflight_outputs
from impodo.domain.shared.models import (
    BusinessReference,
    canonical_json_bytes,
    portable_value,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution.test_incoming_updates import (
    _comparison_evidence,
)


class ReviewRelationshipValueTests(unittest.TestCase):
    def test_saved_workbook_uses_the_compared_name_and_preserves_link_evidence(self):
        shapes = (
            ("account.payment.term", "many2one", False, False),
            ("account.payment.term", "many2one", True, False),
            ("x.shipping.term", "many2many", False, True),
            ("x.shipping.term", "many2many", True, True),
        )
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for model, kind, hybrid, scoped in shapes:
                    with self.subTest(
                        major=major, mode=mode, model=model, hybrid=hybrid
                    ):
                        frozen, result, field = _comparison_evidence(
                            f"{major}.0", mode, model, kind, hybrid, scoped
                        )
                        before_hash = result.semantic_hash
                        before_records = canonical_json_bytes(
                            [
                                portable_value(record.references)
                                for record in frozen.prepared.records
                            ]
                        )
                        with tempfile.TemporaryDirectory() as directory:
                            manifest, path = write_preflight_outputs(
                                result,
                                directory,
                                prepared_records=frozen.prepared.records,
                            )
                            self.assertEqual(
                                manifest.read_text().strip(),
                                canonical_json_bytes(
                                    result.to_portable_dict()
                                ).decode(),
                            )
                            workbook = load_workbook(path)
                            try:
                                records = workbook["Records to load"]
                                headers = [cell.value for cell in records[3]]
                                column = (
                                    headers.index(field.replace("_", " ").title()) + 1
                                )
                                customers = [
                                    row
                                    for row in records.iter_rows(min_row=4)
                                    if row[2].value == "Customers"
                                ]
                                self.assertEqual(len(customers), 2)
                                for row in customers:
                                    cell = row[column - 1]
                                    self.assertEqual(cell.value, "30JJNT")
                                    self.assertIsNotNone(cell.comment)
                                    self.assertIn(
                                        "Relationship lookup key: 30jjnt",
                                        cell.comment.text,
                                    )
                                    self.assertIn(
                                        "Reviewed linked record: 30JJNT",
                                        cell.comment.text,
                                    )
                                evidence = [
                                    row
                                    for row in workbook["Evidence"].iter_rows(
                                        min_row=4, values_only=True
                                    )
                                    if row[0] == "Relationship"
                                    and row[1] == "Customers"
                                ]
                                self.assertEqual(len(evidence), 1)
                                self.assertEqual(evidence[0][4], "30JJNT")
                                self.assertIn(
                                    "Relationship lookup key: 30jjnt", evidence[0][6]
                                )
                                changes = workbook["Changes to Odoo"]
                                self.assertEqual(changes["E4"].value, "30JJNT")
                            finally:
                                workbook.close()
                        self.assertEqual(result.semantic_hash, before_hash)
                        self.assertEqual(
                            canonical_json_bytes(
                                [
                                    portable_value(record.references)
                                    for record in frozen.prepared.records
                                ]
                            ),
                            before_records,
                        )

    def test_unresolved_reference_keeps_its_lookup_key(self):
        frozen, result, field = _comparison_evidence(
            "20.0",
            OdooConnectionMode.REMOTE,
            "account.payment.term",
            "many2one",
            False,
            False,
        )
        result = replace(
            result,
            reference_resolutions=tuple(
                replace(item, status="MISSING", match_count=0)
                for item in result.reference_resolutions
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            _, path = write_preflight_outputs(
                result, directory, prepared_records=frozen.prepared.records
            )
            workbook = load_workbook(path)
            try:
                records = workbook["Records to load"]
                headers = [cell.value for cell in records[3]]
                column = headers.index(field.replace("_", " ").title()) + 1
                customers = [
                    row
                    for row in records.iter_rows(min_row=4)
                    if row[2].value == "Customers"
                ]
                self.assertEqual(
                    [row[column - 1].value for row in customers], ["30jjnt", "30jjnt"]
                )
            finally:
                workbook.close()

    def test_target_first_display_preserves_reviewed_mixed_case(self):
        frozen, result, field = _comparison_evidence(
            "20.0",
            OdooConnectionMode.REMOTE,
            "account.payment.term",
            "many2one",
            True,
            False,
        )
        result = replace(
            result,
            reference_resolutions=tuple(
                replace(
                    item,
                    status="RESOLVED_TARGET",
                    resolved_target=BusinessReference(
                        "account.payment.term", ("Net 30",)
                    ),
                )
                for item in result.reference_resolutions
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            _, path = write_preflight_outputs(
                result, directory, prepared_records=frozen.prepared.records
            )
            workbook = load_workbook(path)
            try:
                records = workbook["Records to load"]
                headers = [cell.value for cell in records[3]]
                column = headers.index(field.replace("_", " ").title()) + 1
                customers = [
                    row
                    for row in records.iter_rows(min_row=4)
                    if row[2].value == "Customers"
                ]
                self.assertEqual(
                    [row[column - 1].value for row in customers], ["Net 30", "Net 30"]
                )
            finally:
                workbook.close()


if __name__ == "__main__":
    unittest.main()
