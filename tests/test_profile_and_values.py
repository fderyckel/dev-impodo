from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import yaml

from uc_migration_profiler.canonical import (
    ValueParseError,
    parse_value,
    values_equal,
)
from uc_migration_profiler.profile import (
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


class ProfileTests(unittest.TestCase):
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
