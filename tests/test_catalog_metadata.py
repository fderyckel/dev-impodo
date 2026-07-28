from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from uc_migration_profiler.catalog import TargetCatalog
from uc_migration_profiler.connectors import SnapshotConnector
from uc_migration_profiler.metadata import validate_profile_metadata
from uc_migration_profiler.models import FieldMetadata, ModelMetadata, TargetRecord
from uc_migration_profiler.planner import plan_metadata_requests
from uc_migration_profiler.profile import load_profile


ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_duplicate_business_keys_are_preserved(self) -> None:
        catalog = TargetCatalog(
            {
                "x.model": (
                    TargetRecord("x.model", 1, {"code": "DUP"}),
                    TargetRecord("x.model", 2, {"code": "DUP"}),
                )
            }
        )
        self.assertEqual(
            len(catalog.find_by_fields("x.model", ("code",), ("DUP",))),
            2,
        )

    def test_relation_id_becomes_business_reference(self) -> None:
        catalog = TargetCatalog(
            {"uom.uom": (TargetRecord("uom.uom", 10, {"code": "KG"}),)}
        )
        reference = catalog.reference_from_id(
            "uom.uom", [10, "Kilogram"], ("code",)
        )
        self.assertEqual(reference.key, ("KG",))


class MetadataValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_profile(
            ROOT / "profiles/examples/golden_slice_v2.yaml"
        )
        connector = SnapshotConnector(
            combined_path=ROOT / "fixtures/golden/target_snapshot.json"
        )
        self.snapshot = connector.get_model_metadata(
            plan_metadata_requests(self.profile)
        )

    def test_golden_metadata_is_complete(self) -> None:
        issues, coverage = validate_profile_metadata(self.profile, self.snapshot)
        self.assertEqual(issues, ())
        self.assertTrue(all(item["status"] == "COMPLETE" for item in coverage))

    def test_readonly_proposed_field_is_rejected(self) -> None:
        product = self.snapshot.models["product.template"]
        fields = dict(product.fields)
        fields["name"] = replace(fields["name"], readonly=True)
        models = dict(self.snapshot.models)
        models["product.template"] = replace(product, fields=fields)
        issues, _ = validate_profile_metadata(
            self.profile, replace(self.snapshot, models=models)
        )
        self.assertIn("TARGET_FIELD_READONLY", {issue.code for issue in issues})

    def test_incorrect_relation_metadata_is_rejected(self) -> None:
        product = self.snapshot.models["product.template"]
        fields = dict(product.fields)
        fields["uom_id"] = replace(
            fields["uom_id"], type="many2many", relation="wrong.model"
        )
        models = dict(self.snapshot.models)
        models["product.template"] = replace(product, fields=fields)
        issues, _ = validate_profile_metadata(
            self.profile, replace(self.snapshot, models=models)
        )
        codes = {issue.code for issue in issues}
        self.assertIn("TARGET_RELATION_KIND_INCORRECT", codes)
        self.assertIn("TARGET_RELATED_MODEL_INCORRECT", codes)

    def test_missing_target_only_reference_model_is_rejected(self) -> None:
        models = dict(self.snapshot.models)
        models.pop("uom.uom")
        issues, _ = validate_profile_metadata(
            self.profile, replace(self.snapshot, models=models)
        )
        self.assertTrue(
            any(
                issue.code == "TARGET_MODEL_UNKNOWN"
                and "uom.uom" in issue.message
                for issue in issues
            )
        )


if __name__ == "__main__":
    unittest.main()
