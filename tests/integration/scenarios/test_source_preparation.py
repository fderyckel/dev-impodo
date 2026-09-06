from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.adapters.scenarios.source_preparation import prepare_scenario_sources
from impodo.domain.compiler import compile_profile_document
from impodo.domain.recipe.profile import ProfileDocument
from impodo.domain.scenarios import CombinedDistinctTable


class ScenarioSourcePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[3] / ".tmp"
        root.mkdir(exist_ok=True)
        self.fixture = root / f"scenario-combined-{uuid4()}"
        self.fixture.mkdir()
        (self.fixture / "articles.csv").write_text(
            "UnitId\nPCE\ng\nPCE\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.fixture / "bom-lines.csv").write_text(
            "UnitId\nG\ng\nPCE\n",
            encoding="utf-8",
            newline="\n",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.fixture)

    def test_combines_normalizes_and_deduplicates_before_mapping(self) -> None:
        profile = ProfileDocument.model_validate(
            {
                "profile": {"id": "combined_source_test"},
                "datasets": [
                    {
                        "name": "uoms",
                        "source": {"file": "articles.csv"},
                        "target": {"model": "uom.uom", "mode": "upsert"},
                        "source_identity": {"fields": ["UnitId"]},
                        "target_identity": {
                            "components": [
                                {
                                    "source_fields": ["UnitId"],
                                    "target_fields": ["name"],
                                }
                            ]
                        },
                        "fields": {
                            "active": {
                                "source": "UnitId",
                                "formula": "True",
                                "type": "boolean",
                            }
                        },
                    }
                ],
            }
        )
        rule = CombinedDistinctTable.model_validate(
            {
                "output_dataset": "uoms",
                "output_field": "UnitId",
                "formula": "'g' if lower(value) == 'g' else upper(value)",
                "inputs": [
                    {"file": "articles.csv", "field": "UnitId"},
                    {"file": "bom-lines.csv", "field": "UnitId"},
                ],
            }
        )

        prepared = prepare_scenario_sources(
            compile_profile_document(profile),
            self.fixture,
            (rule,),
        )

        self.assertEqual(
            tuple(record.source_identity for record in prepared.records),
            (("PCE",), ("g",)),
        )
        self.assertEqual(prepared.issues, ())
        self.assertIn("derived/uoms", prepared.source_hashes)


if __name__ == "__main__":
    unittest.main()
