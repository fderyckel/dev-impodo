"""Profile-formula tests for deterministic source preparation."""

from __future__ import annotations

from decimal import Decimal
import unittest

from impodo.domain.compiler import compile_profile_document
from impodo.domain.preparation.source import (
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.profile import ProfileDocument


def _profile() -> ProfileDocument:
    return ProfileDocument.model_validate(
        {
            "profile": {"id": "formula_profile"},
            "datasets": [
                {
                    "name": "lines",
                    "source": {"file": "lines.csv"},
                    "target": {"model": "mrp.bom.line"},
                    "source_identity": {"fields": ["line"]},
                    "target_identity": {
                        "components": [
                            {
                                "source_fields": ["line"],
                                "target_fields": ["sequence"],
                                "type": "integer",
                            }
                        ]
                    },
                    "fields": {
                        "product_qty": {
                            "source": "quantity",
                            "formula": "quantity / series * 1000",
                            "type": "decimal",
                            "required": True,
                            "normalize": {"decimal_places": 3},
                        }
                    },
                }
            ],
        }
    )


class ProfileFormulaTests(unittest.TestCase):
    def test_safe_formula_uses_the_complete_source_row(self) -> None:
        plan = compile_profile_document(_profile())
        table = SourceTable(
            dataset="lines",
            path=plan.datasets[0].source.file,
            headers=("line", "quantity", "series"),
            rows=(
                SourceRow(
                    number=2,
                    values={"line": 1, "quantity": 2.95, "series": 1},
                ),
            ),
            content_hash="sha256:" + "1" * 64,
        )

        prepared = prepare_source_tables(
            plan,
            (table,),
            source_hashes={"lines.csv": table.content_hash},
        )

        self.assertEqual(
            prepared.records[0].scalar_values["product_qty"],
            Decimal("2950.000"),
        )
        self.assertFalse(prepared.records[0].issues)

    def test_formula_failure_is_a_blocking_row_issue(self) -> None:
        plan = compile_profile_document(_profile())
        table = SourceTable(
            dataset="lines",
            path=plan.datasets[0].source.file,
            headers=("line", "quantity", "series"),
            rows=(
                SourceRow(
                    number=2,
                    values={"line": 1, "quantity": 2.95, "series": 0},
                ),
            ),
            content_hash="sha256:" + "1" * 64,
        )

        prepared = prepare_source_tables(
            plan,
            (table,),
            source_hashes={"lines.csv": table.content_hash},
        )

        self.assertEqual(
            prepared.records[0].issues[0].code,
            "SOURCE_FORMULA_INVALID",
        )
        self.assertTrue(prepared.records[0].blocked)


if __name__ == "__main__":
    unittest.main()
