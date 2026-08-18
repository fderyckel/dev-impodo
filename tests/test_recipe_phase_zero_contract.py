"""Freeze the proposed reusable-recipe Phase 0 contracts and examples."""

from __future__ import annotations

from copy import deepcopy
import csv
from decimal import Decimal
import json
from pathlib import Path
import re
import unittest

from impodo.domain.serialization import canonical_json, content_hash
from impodo.models import assert_no_numeric_odoo_ids


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "recipes" / "phase-0"
HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
UUID_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _load_json(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _load_csv(name: str) -> tuple[dict[str, str], ...]:
    with (FIXTURES / name).open(encoding="utf-8", newline="") as stream:
        return tuple(csv.DictReader(stream))


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


class RecipePhaseZeroContractTests(unittest.TestCase):
    """Keep future recipe decisions reviewable before runtime implementation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.envelope = _load_json("customer-recipe-v1.json")
        cls.acceptance = _load_json("acceptance-contract.json")
        cls.reference = _load_json("customer-type-reference.json")
        cls.targets = _load_json("target-variants.json")

    def test_recipe_and_evidence_hashes_are_canonical(self) -> None:
        self.assertEqual(
            set(self.envelope),
            {
                "recipe_contract_version",
                "semantic_hash",
                "payload_hash",
                "recipe",
                "compatibility_hints",
                "provenance",
            },
        )
        recipe = self.envelope["recipe"]
        self.assertEqual(
            set(recipe),
            {
                "contract_versions",
                "source_shape",
                "source_preparation",
                "mapping",
                "target_governance",
                "quality",
                "reference_dependencies",
                "control_definitions",
            },
        )
        self.assertEqual(self.envelope["semantic_hash"], content_hash(recipe))
        self.assertEqual(
            self.envelope["payload_hash"],
            content_hash(
                {
                    key: value
                    for key, value in self.envelope.items()
                    if key != "payload_hash"
                }
            ),
        )
        self.assertRegex(str(self.envelope["semantic_hash"]), HASH)
        self.assertRegex(str(self.envelope["payload_hash"]), HASH)

        reference_without_hash = {
            key: value
            for key, value in self.reference.items()
            if key != "content_hash"
        }
        self.assertEqual(
            self.reference["content_hash"],
            content_hash(reference_without_hash),
        )
        recipe_reference = recipe["reference_dependencies"]["references"][0]
        self.assertEqual(
            recipe_reference["content_hash"],
            self.reference["content_hash"],
        )

        coverage = self.acceptance["categorical_coverage_evidence"]
        coverage_without_hash = {
            key: value for key, value in coverage.items() if key != "content_hash"
        }
        self.assertEqual(
            coverage["content_hash"],
            content_hash(coverage_without_hash),
        )

    def test_semantic_identity_excludes_provenance_and_physical_ids(self) -> None:
        changed = deepcopy(self.envelope)
        changed["provenance"]["origin_project_id"] = (
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        )
        changed["provenance"]["publisher"]["subject_id"] = "bootstrap-operator"
        changed["compatibility_hints"]["datasets"][0]["prior_display_name"] = (
            "Customer export"
        )

        self.assertEqual(
            content_hash(changed["recipe"]),
            self.envelope["semantic_hash"],
        )
        self.assertNotEqual(
            content_hash(
                {
                    key: value
                    for key, value in changed.items()
                    if key != "payload_hash"
                }
            ),
            self.envelope["payload_hash"],
        )

        semantic_text = json.dumps(self.envelope["recipe"], sort_keys=True)
        self.assertIsNone(UUID_TEXT.search(semantic_text))
        for forbidden in (
            "project_id",
            "series_id",
            "recipe_id",
            "mapping_id",
            "reference_id",
            "source_column_key",
            "ordinal",
            "storage_key",
            "file_path",
            "credential",
        ):
            self.assertNotIn(f'"{forbidden}"', semantic_text)
        for value in _walk(self.envelope["recipe"]):
            if not isinstance(value, dict):
                continue
            for key, item in value.items():
                if key in {"dataset_id", "output_dataset_id"}:
                    self.assertTrue(str(item).startswith("dataset:"))
                if key == "column_id":
                    self.assertTrue(str(item).startswith("column:"))
        assert_no_numeric_odoo_ids(self.envelope["recipe"])

    def test_semantic_collections_have_frozen_deterministic_order(self) -> None:
        recipe = self.envelope["recipe"]
        dataset = recipe["source_shape"]["datasets"][0]
        column_ids = [item["logical_column_id"] for item in dataset["columns"]]
        self.assertEqual(column_ids, sorted(column_ids))

        mapping = recipe["mapping"]["datasets"][0]
        field_ids = [item["logical_field_id"] for item in mapping["fields"]]
        self.assertEqual(field_ids, sorted(field_ids))
        relationship_matches = mapping["relationships"][0]["value_matches"]
        self.assertEqual(
            [item["source_value"] for item in relationship_matches],
            ["BEL", "FRA"],
        )
        language_matches = mapping["fields"][1]["value_matches"]
        self.assertEqual(
            [item["source_value"] for item in language_matches],
            ["English", "French"],
        )

        dependency_ids = [
            item["logical_dependency_id"]
            for item in recipe["target_governance"]["dependencies"]
        ]
        self.assertEqual(dependency_ids, sorted(dependency_ids))
        self.assertEqual(
            [item["key"][0] for item in self.reference["entries"]],
            ["Company", "Individual"],
        )

    def test_customer_editions_freeze_row_delta_and_fresh_totals(self) -> None:
        first = {
            row["customer_code"]: row
            for row in _load_csv("customer-data-version-1.csv")
        }
        second = {
            row["customer_code"]: row
            for row in _load_csv("customer-data-version-2.csv")
        }
        common_fields = tuple(next(iter(first.values())))

        added = sorted(set(second) - set(first))
        absent = sorted(set(first) - set(second))
        unchanged = sorted(
            key
            for key in set(first) & set(second)
            if all(first[key][field] == second[key][field] for field in common_fields)
        )
        changed = sorted((set(first) & set(second)) - set(unchanged))
        expected = self.acceptance["expected_row_delta"]

        self.assertEqual(added, expected["added"])
        self.assertEqual(changed, expected["changed"])
        self.assertEqual(unchanged, expected["unchanged"])
        self.assertEqual(absent, expected["absent_without_delete"])
        self.assertEqual(
            sum(Decimal(row["open_balance"]) for row in first.values()),
            Decimal("4700000.00"),
        )
        self.assertEqual(
            sum(Decimal(row["open_balance"]) for row in second.values()),
            Decimal("5100000.00"),
        )

        recipe_control = self.envelope["recipe"]["control_definitions"][
            "controls"
        ][0]
        self.assertNotIn("expected_total", recipe_control)
        self.assertNotIn("expected_value", recipe_control)
        expectations = self.acceptance["edition_control_expectations"]
        self.assertEqual(
            [item["expected_value"] for item in expectations],
            ["4700000.00", "5100000.00"],
        )

    def test_lifecycle_application_and_intent_contracts_are_separated(self) -> None:
        series = self.acceptance["series"]
        project_ids = {item["project_id"] for item in self.acceptance["editions"]}
        self.assertNotIn(series["series_id"], project_ids)
        self.assertNotEqual(
            series["current_registered_project_id"],
            series["pending_project_id"],
        )

        application = self.acceptance["application_draft"]
        self.assertEqual(application["state"], "BINDING")
        self.assertEqual(
            application["column_overrides"],
            {"column:customers.customer_code": "column_3_account_number"},
        )
        for forbidden in (
            "mapping",
            "fields",
            "relationships",
            "approved_write_fields",
            "approval",
            "execution",
        ):
            self.assertNotIn(forbidden, application)

        intents = {
            item["kind"]: item for item in self.acceptance["intent_examples"]
        }
        self.assertEqual(
            set(intents),
            {
                "EDITION_CREATION",
                "RECIPE_PUBLICATION",
                "CREDENTIAL_COPY",
                "SERIES_DELETION",
            },
        )
        self.assertEqual(intents["CREDENTIAL_COPY"]["role"], "READ")
        self.assertNotIn("secret", json.dumps(intents, sort_keys=True).casefold())
        self.assertEqual(
            intents["SERIES_DELETION"]["state"],
            "TARGETS_ENUMERATED",
        )
        self.assertTrue(intents["SERIES_DELETION"]["targets"])

    def test_phase_zero_scenarios_and_bounds_are_complete(self) -> None:
        expected_names = {
            "compatible_replacement",
            "added_changed_unchanged_absent",
            "new_language_german",
            "new_country_lux",
            "stale_target_language_choice",
            "missing_country_business_key",
            "ambiguous_custom_many2one_key",
            "renamed_customer_code",
            "renamed_customer_code_after_override",
            "reordered_logical_formula",
            "ordinal_formula_not_rewritten",
            "duplicate_source_header",
            "candidate_type_drift_with_supported_decimal_transform",
            "reference_package_changed",
            "manager_quality_rule_reapplied",
            "new_unused_column",
            "new_control_expectation_required",
        }
        scenarios = {
            item["scenario"]: item
            for item in self.acceptance["expected_scenarios"]
        }
        self.assertEqual(set(scenarios), expected_names)
        self.assertEqual(
            scenarios["candidate_type_drift_with_supported_decimal_transform"][
                "severity"
            ],
            "NEEDS_REVIEW",
        )
        self.assertEqual(
            scenarios["ordinal_formula_not_rewritten"]["code"],
            "RECIPE_FORMULA_NOT_PORTABLE",
        )

        target_variants = {
            item["name"]: item["expected_code"]
            for item in self.targets["variants"]
        }
        self.assertEqual(
            target_variants,
            {
                "stale_target_language_choice": "MAPPING_SELECTION_VALUE_INVALID",
                "missing_country_business_key": (
                    "MAPPING_RELATIONSHIP_TARGET_MISSING"
                ),
                "ambiguous_custom_many2one_key": (
                    "MAPPING_RELATIONSHIP_TARGET_AMBIGUOUS"
                ),
            },
        )
        self.assertEqual(
            self.acceptance["contract_versions"]["required_mapping_contract"],
            11,
        )
        bounds = self.acceptance["bounds"]
        self.assertLessEqual(
            len(canonical_json(self.envelope).encode("utf-8")),
            bounds["main_recipe_bytes"],
        )
        self.assertEqual(bounds["value_matches_per_field"], 1_000)
        self.assertEqual(bounds["distinct_values_per_field"], 1_000)

    def test_drift_csv_variants_are_frozen(self) -> None:
        with (FIXTURES / "customer-data-version-2-duplicate-header.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            headers = next(csv.reader(stream))
        self.assertNotEqual(len(headers), len(set(headers)))

        renamed = _load_csv("customer-data-version-2-renamed-reordered.csv")
        self.assertIn("account_number", renamed[0])
        self.assertNotIn("customer_code", renamed[0])
        self.assertEqual(next(iter(renamed[0])), "marketing_segment")

        drifted = _load_csv("customer-data-version-2-type-drift.csv")
        self.assertTrue(all("," in row["open_balance"] for row in drifted))


if __name__ == "__main__":
    unittest.main()
