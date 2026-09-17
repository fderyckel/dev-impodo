from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import unittest
from uuid import uuid4

from openpyxl import load_workbook

from impodo.web.composition.cli import main


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "scenarios" / "plw-manufacturing-preparation" / "v1"
FIXTURES = SCENARIO / "fixtures"


def rows(filename: str) -> tuple[tuple, list[tuple]]:
    book = load_workbook(FIXTURES / filename, read_only=True, data_only=True)
    values = book.active.values
    headers = next(values)
    records = list(values)
    book.close()
    return headers, records


def key(value: object) -> str:
    return str(value).strip() if value is not None else ""


class PlwManufacturingPreparationScenarioTests(unittest.TestCase):
    def test_all_four_realistic_files_prepare_without_target_or_write(self) -> None:
        temporary_root = ROOT / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        result_path = temporary_root / f"plw-scenario-{uuid4()}.json"
        try:
            exit_code = main(
                [
                    "scenario",
                    "run",
                    "--definition",
                    str(SCENARIO / "scenario.yaml"),
                    "--connector",
                    "none",
                    "--output",
                    str(result_path),
                ]
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
        finally:
            result_path.unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["preparation"], {"prepared_rows": 37253, "source_issues": 0})
        self.assertEqual(result["write_attempt_count"], 0)
        self.assertIsNone(result["target"]["target_hash"])

    def test_fixture_graph_has_complete_boms_and_stable_operation_keys(self) -> None:
        ph, products = rows("PLW_demo_active_products.xlsx")
        bh, boms = rows("PLW_demo_active_boms.xlsx")
        lh, lines = rows("PLW_demo_active_bom_lines.xlsx")
        oh, operations = rows("PLW_demo_active_bom_operations.xlsx")

        self.assertEqual(
            (len(products), len(boms), len(lines), len(operations)),
            (2841, 1942, 15142, 17328),
        )
        self.assertEqual((len(ph), len(bh), len(lh), len(oh)), (20, 7, 19, 25))

        product_ids = {key(row[0]) for row in products}
        bom_by_id = {key(row[bh.index("BOMId")]): row for row in boms}
        bom_ids = set(bom_by_id)
        self.assertEqual(len(product_ids), len(products))
        self.assertEqual(len(bom_ids), len(boms))
        self.assertTrue(
            all(
                key(row[bh.index("Active")]) == "Oui"
                and key(row[bh.index("Approved")]) == "Oui"
                for row in boms
            )
        )
        self.assertTrue(all(key(row[bh.index("ItemId")]) in product_ids for row in boms))

        line_boms = {key(row[lh.index("BOMId")]) for row in lines}
        operation_boms = {key(row[oh.index("BOMId")]) for row in operations}
        self.assertEqual(line_boms, bom_ids)
        self.assertEqual(operation_boms, bom_ids)
        self.assertTrue(all(key(row[lh.index("ItemId")]) in product_ids for row in lines))
        self.assertEqual(
            len(
                {
                    (key(row[lh.index("BOMId")]), key(row[lh.index("LineNum")]))
                    for row in lines
                }
            ),
            len(lines),
        )
        self.assertTrue(all(row[lh.index("BOMQty")] > 0 for row in lines))
        zero_series_ids = {
            key(row[lh.index("RecId")])
            for row in lines
            if row[lh.index("BOMQtySerie")] == 0
        }
        self.assertEqual(zero_series_ids, {"5637245187", "5637245822"})
        self.assertEqual(
            {key(row[lh.index("UnitId")]) for row in lines},
            {"PCE", "G", "g", "KG", "kg", "ML", "ml"},
        )

        workcenters_by_item: dict[str, set[str]] = defaultdict(set)
        for row in operations:
            bom_id = key(row[oh.index("BOMId")])
            item_id = key(row[oh.index("Item Id")])
            self.assertEqual(item_id, key(bom_by_id[bom_id][bh.index("ItemId")]))
            workcenters_by_item[item_id].add(key(row[oh.index("Demo Work Center")]))
        self.assertTrue(
            all(len(codes) == 1 for codes in workcenters_by_item.values())
        )
        workcenter_codes = set().union(*workcenters_by_item.values())
        self.assertEqual(len(workcenter_codes), 27)
        self.assertTrue(
            all(re.fullmatch(r"[ABC]0[1-9]", code) for code in workcenter_codes)
        )
        self.assertEqual(
            len(
                {
                    (key(row[oh.index("BOMId")]), key(row[oh.index("Opr Id")]))
                    for row in operations
                }
            ),
            len(operations),
        )


if __name__ == "__main__":
    unittest.main()
