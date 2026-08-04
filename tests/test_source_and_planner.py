from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from openpyxl import Workbook
from pydantic import ValidationError

from impodo.models import LogicalReference
from impodo.planner import (
    plan_metadata_requests,
    plan_record_requests,
)
from impodo.profile import SourceSpec, load_profile
from impodo.source import SourceLoadError, prepare_sources


ROOT = Path(__file__).resolve().parents[1]


class PreparedRecordTests(unittest.TestCase):
    def test_bom_values_and_references_are_preserved(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/bom.yaml")
        bundle = prepare_sources(profile, ROOT / "examples/bom")
        self.assertEqual(len(bundle.records), 3)
        line = next(record for record in bundle.records if record.dataset == "bom_lines")
        self.assertEqual(line.target_identity[1], 10)
        self.assertEqual(line.scalar_values["product_qty"], Decimal("0.4500"))
        self.assertIsInstance(line.target_identity[0], LogicalReference)
        self.assertIsInstance(line.references["bom_id"], LogicalReference)
        self.assertNotIn("odoo", repr(line).casefold())

    def test_duplicate_source_identity_blocks_all_duplicates(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/bom.yaml")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(ROOT / "examples/bom", target / "input")
            lines = target / "input/bom_lines.csv"
            lines.write_text(lines.read_text() + "BOM-001-L10,BOM-001,30,CAP-X,1.0\n")
            bundle = prepare_sources(profile, target / "input")
            duplicates = [
                record
                for record in bundle.records
                if record.dataset == "bom_lines"
                and record.source_identity == ("BOM-001-L10",)
            ]
            self.assertEqual(len(duplicates), 2)
            self.assertTrue(all(record.blocked for record in duplicates))
            self.assertTrue(
                all(
                    any(
                        issue.code == "SOURCE_IDENTITY_DUPLICATE"
                        for issue in record.issues
                    )
                    for record in duplicates
                )
            )

    def test_xlsx_sheet_is_prepared_with_native_cell_types(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/products.yaml")
        products = profile.dataset("products")
        source = products.source.model_copy(
            update={
                "file": "products.xlsx",
                "sheet": "Products",
                "header_row": 3,
            }
        )
        xlsx_profile = profile.model_copy(
            update={
                "datasets": (
                    products.model_copy(update={"source": source}),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Products"
            worksheet.append(["Export generated for a governed test"])
            worksheet.append([])
            worksheet.append(
                [
                    "article_code",
                    "company_code",
                    "description",
                    "active",
                    "uom_code",
                ]
            )
            worksheet.append(["P-001", "BE", "Product one", True, "UNIT"])
            workbook.save(target / "products.xlsx")

            bundle = prepare_sources(xlsx_profile, target)

        self.assertEqual(len(bundle.records), 1)
        self.assertEqual(bundle.records[0].source_row, 4)
        self.assertEqual(bundle.records[0].source_identity, ("P-001", "BE"))
        self.assertIs(bundle.records[0].scalar_values["active"], True)
        self.assertEqual(set(bundle.source_hashes), {"products.xlsx"})

    def test_xlsx_formula_cells_are_rejected(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/products.yaml")
        products = profile.dataset("products")
        source = products.source.model_copy(
            update={"file": "products.xlsx", "sheet": "Products"}
        )
        xlsx_profile = profile.model_copy(
            update={
                "datasets": (
                    products.model_copy(update={"source": source}),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Products"
            worksheet.append(
                [
                    "article_code",
                    "company_code",
                    "description",
                    "active",
                    "uom_code",
                ]
            )
            worksheet.append(["P-001", "BE", "=1+1", True, "UNIT"])
            workbook.save(target / "products.xlsx")

            with self.assertRaisesRegex(SourceLoadError, "formula cell rejected"):
                prepare_sources(xlsx_profile, target)

    def test_xlsx_missing_sheet_and_arbitrary_zip_are_rejected(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/products.yaml")
        products = profile.dataset("products")
        source = products.source.model_copy(
            update={"file": "products.xlsx", "sheet": "Products"}
        )
        xlsx_profile = profile.model_copy(
            update={
                "datasets": (
                    products.model_copy(update={"source": source}),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            workbook = Workbook()
            workbook.active.title = "Other sheet"
            workbook.save(target / "products.xlsx")
            with self.assertRaisesRegex(SourceLoadError, "does not exist"):
                prepare_sources(xlsx_profile, target)

            with zipfile.ZipFile(target / "products.xlsx", "w") as archive:
                archive.writestr("not-an-office-file.txt", "data")
            with self.assertRaisesRegex(SourceLoadError, "valid XLSX container"):
                prepare_sources(xlsx_profile, target)

    def test_duplicate_csv_headers_are_rejected(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/products.yaml")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "products.csv").write_text(
                "article_code,company_code,description,active,uom_code,uom_code\n"
                "P-001,BE,Product one,true,UNIT,UNIT\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourceLoadError, "duplicate headers"):
                prepare_sources(profile, target)

    def test_source_profile_rejects_legacy_and_escaping_paths(self) -> None:
        with self.assertRaisesRegex(ValidationError, ".csv or .xlsx"):
            SourceSpec(file="legacy.xls")
        with self.assertRaisesRegex(ValidationError, "contained relative path"):
            SourceSpec(file="../outside.csv")
        with self.assertRaisesRegex(ValidationError, "source.sheet is required"):
            SourceSpec(file="products.xlsx")


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_profile(
            ROOT / "profiles/examples/golden_slice.yaml"
        )
        self.bundle = prepare_sources(self.profile, ROOT / "examples/golden")

    def test_metadata_requests_only_profile_models_and_fields(self) -> None:
        requests = plan_metadata_requests(self.profile)
        by_model = {request.model: set(request.fields) for request in requests}
        self.assertIn("product.template", by_model)
        self.assertEqual(
            by_model["uom.uom"],
            {"x_external_code"},
        )
        self.assertNotIn("message_ids", by_model["product.template"])

    def test_record_requests_are_batched_by_model(self) -> None:
        requests = plan_record_requests(self.profile, self.bundle.records)
        models = [request.model for request in requests]
        self.assertEqual(len(models), len(set(models)))
        self.assertLess(len(models), len(self.bundle.records))
        product_request = next(
            request for request in requests if request.model == "product.template"
        )
        self.assertIn("default_code", product_request.fields)
        self.assertNotIn("description_sale", product_request.fields)

    def test_target_domain_restriction_is_preserved(self) -> None:
        products = self.profile.dataset("products")
        changed_products = products.model_copy(
            update={"target_domain": (["active", "=", True],)}
        )
        changed_profile = self.profile.model_copy(
            update={
                "datasets": tuple(
                    changed_products if item.name == "products" else item
                    for item in self.profile.datasets
                )
            }
        )
        request = next(
            item
            for item in plan_record_requests(changed_profile, self.bundle.records)
            if item.model == "product.template"
        )
        self.assertIn(["active", "=", True], request.domain)

    def test_datasets_for_same_model_share_one_or_domain(self) -> None:
        assets = self.profile.dataset("assets")
        changed_assets = assets.model_copy(
            update={
                "target": assets.target.model_copy(
                    update={"model": "res.partner"}
                )
            }
        )
        changed_profile = self.profile.model_copy(
            update={
                "datasets": tuple(
                    changed_assets if item.name == "assets" else item
                    for item in self.profile.datasets
                )
            }
        )

        requests = plan_record_requests(changed_profile, self.bundle.records)
        partner_requests = [
            item for item in requests if item.model == "res.partner"
        ]

        self.assertEqual(len(partner_requests), 1)
        self.assertEqual(partner_requests[0].domain[0], "|")
        domain_fields = {
            item[0]
            for item in partner_requests[0].domain
            if isinstance(item, list)
        }
        self.assertEqual(domain_fields, {"ref", "code"})


if __name__ == "__main__":
    unittest.main()
