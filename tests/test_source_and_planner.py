from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import shutil
import tempfile
import unittest

from uc_migration_profiler.models import LogicalReference
from uc_migration_profiler.planner import (
    plan_metadata_requests,
    plan_record_requests,
)
from uc_migration_profiler.profile import load_profile
from uc_migration_profiler.source import prepare_sources


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
            {"x_uc_code"},
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


if __name__ == "__main__":
    unittest.main()
