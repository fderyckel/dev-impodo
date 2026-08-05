from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import yaml

from impodo.canonical import (
    ValueParseError,
    parse_value,
    values_equal,
)
from impodo.domain.compiler import CompiledMigrationPlan, compile_profile_document
from impodo.models import (
    BusinessReference,
    LogicalReference,
    portable_value,
    restore_portable_value,
)
from impodo.profile import (
    NormalizationSpec,
    ProfileLoadError,
    load_profile,
)


ROOT = Path(__file__).resolve().parents[1]


class CanonicalValueTests(unittest.TestCase):
    def test_all_supported_types_are_typed(self) -> None:
        policy = NormalizationSpec(trim=True, decimal_places=2)
        self.assertEqual(parse_value(" text ", "string", policy), "text")
        self.assertEqual(parse_value("42", "integer", policy), 42)
        self.assertEqual(parse_value("1.235", "decimal", policy), Decimal("1.24"))
        self.assertIs(parse_value("false", "boolean", policy), False)
        self.assertEqual(
            parse_value("2026-07-28", "date", policy),
            date(2026, 7, 28),
        )
        self.assertEqual(
            parse_value("2026-07-28T10:00:00Z", "datetime", policy),
            datetime(2026, 7, 28, 10, tzinfo=timezone.utc),
        )
        self.assertIsNone(parse_value("", "string", policy))

    def test_boolean_is_not_truthiness(self) -> None:
        policy = NormalizationSpec()
        with self.assertRaises(ValueParseError):
            parse_value("sometimes", "boolean", policy)

    def test_null_policies(self) -> None:
        self.assertTrue(values_equal(None, "", "equivalent"))
        self.assertFalse(values_equal(None, "", "distinct"))
        self.assertTrue(values_equal(None, "target", "ignore_source_null"))

    def test_portable_preflight_values_round_trip_losslessly(self) -> None:
        values = {
            "decimal": Decimal("10.2500"),
            "date": date(2026, 8, 5),
            "datetime": datetime(2026, 8, 5, 12, 30, tzinfo=timezone.utc),
            "null": None,
            "boolean": False,
            "integer": 42,
            "string": "FR",
            "logical": LogicalReference(
                origin="target",
                key=("FR",),
                model="res.country",
                target_fields=("code",),
            ),
            "relationships": (
                BusinessReference("res.country", ("FR",)),
                LogicalReference(
                    origin="incoming",
                    key=("PARENT-1",),
                    dataset="parents",
                ),
            ),
        }

        self.assertEqual(
            restore_portable_value(portable_value(values)),
            values,
        )


class ProfileTests(unittest.TestCase):
    def test_profile_compiles_to_deterministic_runtime_contract(self) -> None:
        profile = load_profile(ROOT / "profiles/examples/products.yaml")

        first = compile_profile_document(profile)
        second = compile_profile_document(profile)

        self.assertEqual(first.plan_id, profile.profile.id)
        self.assertEqual(first.origin, "profile_document")
        self.assertEqual(first.datasets, profile.datasets)
        self.assertEqual(first.semantic_hash, second.semantic_hash)
        self.assertEqual(
            CompiledMigrationPlan.from_json(first.to_json()),
            first,
        )
        self.assertFalse(hasattr(first, "profile"))

    def test_example_profiles_validate(self) -> None:
        for path in (ROOT / "profiles/examples").glob("*.yaml"):
            with self.subTest(path=path.name):
                self.assertTrue(load_profile(path).datasets)

    def test_unknown_keys_are_rejected_actionably(self) -> None:
        data = yaml.safe_load(
            (ROOT / "profiles/examples/products.yaml").read_text()
        )
        data["datasets"][0]["fields"]["name"]["compar"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(yaml.safe_dump(data, sort_keys=False))
            with self.assertRaisesRegex(ProfileLoadError, "compar"):
                load_profile(path)

    def test_contradictory_validate_only_is_rejected(self) -> None:
        data = yaml.safe_load((ROOT / "profiles/template.yaml").read_text())
        data["datasets"][0]["fields"]["name"]["validate_only"] = True
        data["datasets"][0]["fields"]["name"]["compare"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(yaml.safe_dump(data, sort_keys=False))
            with self.assertRaisesRegex(ProfileLoadError, "validate_only"):
                load_profile(path)

    def test_deferred_cycle_is_rejected(self) -> None:
        data = yaml.safe_load((ROOT / "profiles/template.yaml").read_text())
        first = data["datasets"][0]
        first["name"] = "one"
        first["source_identity"]["fields"] = ["source_key"]
        first["relations"]["uom_id"]["resolve"] = {
            "dataset": "two",
            "target_source_fields": ["source_key"],
        }
        second = yaml.safe_load(yaml.safe_dump(first))
        second["name"] = "two"
        second["source"]["file"] = "two.csv"
        second["relations"]["uom_id"]["resolve"] = {
            "dataset": "one",
            "target_source_fields": ["source_key"],
        }
        data["datasets"] = [first, second]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cycle.yaml"
            path.write_text(yaml.safe_dump(data, sort_keys=False))
            with self.assertRaisesRegex(ProfileLoadError, "cycle"):
                load_profile(path)

if __name__ == "__main__":
    unittest.main()
