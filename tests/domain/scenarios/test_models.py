from __future__ import annotations

import unittest

from pydantic import ValidationError

from impodo.domain.scenarios import ScenarioDefinition, TargetProjection


def _definition() -> dict[str, object]:
    return {
        "contract_version": 1,
        "scenario_id": "contact-round-trip",
        "purpose": "RELEASE_QUALIFICATION",
        "source": {
            "mode": "FILE",
            "fixture_set": "fixtures/v1",
            "fixture_hash": "sha256:" + "1" * 64,
        },
        "rules": {
            "profile": "profile.yaml",
            "profile_hash": "sha256:" + "2" * 64,
        },
        "destination": {
            "mode": "LOCAL_ODOO",
            "target_profile": "local.contacts",
            "expected_seed": "empty-contacts",
            "relevant_modules": ["base", "contacts"],
        },
        "execution": {
            "stop_after": "REPEAT_COMPARISON",
            "write_policy": "DISPOSABLE_SCENARIO_ONLY",
        },
        "expectations": {
            "target_projection": "expected-target.json",
            "target_projection_hash": "sha256:" + "3" * 64,
            "prepared_rows": 3,
            "first_comparison": {
                "create": 3,
                "update": 0,
                "unchanged": 0,
                "blocked": 0,
                "ambiguous": 0,
            },
            "reconciliation": {
                "verified": 3,
                "fallout": 0,
                "outcome_unknown": 0,
            },
            "repeat_comparison": {
                "create": 0,
                "update": 0,
                "unchanged": 3,
                "blocked": 0,
                "ambiguous": 0,
            },
        },
    }


class ScenarioDefinitionTests(unittest.TestCase):
    def test_valid_write_scenario_is_immutable_and_hashable(self) -> None:
        definition = ScenarioDefinition.model_validate(_definition())

        self.assertEqual(definition.scenario_id, "contact-round-trip")
        self.assertRegex(definition.semantic_hash, r"^sha256:[0-9a-f]{64}$")
        with self.assertRaises(ValidationError):
            definition.scenario_id = "changed"

    def test_unknown_property_is_rejected(self) -> None:
        value = _definition()
        value["unexpected"] = True

        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            ScenarioDefinition.model_validate(value)

    def test_relative_paths_cannot_escape(self) -> None:
        value = _definition()
        value["source"] = {
            "mode": "FILE",
            "fixture_set": "../customer-data",
            "fixture_hash": "sha256:" + "1" * 64,
        }

        with self.assertRaisesRegex(ValidationError, "contained relative path"):
            ScenarioDefinition.model_validate(value)

    def test_file_scenario_accepts_combined_distinct_source_table(self) -> None:
        value = _definition()
        source = dict(value["source"])
        source["combined_distinct_tables"] = [
            {
                "output_dataset": "uoms",
                "output_field": "UnitId",
                "formula": "'g' if lower(value) == 'g' else upper(value)",
                "inputs": [
                    {
                        "file": "DEMO_Article.xlsx",
                        "sheet": "PLW",
                        "field": "UnitId",
                    },
                    {
                        "file": "DEMO_BOM.xlsx",
                        "sheet": "Sheet1",
                        "field": "UnitId",
                    },
                ],
            }
        ]
        value["source"] = source

        definition = ScenarioDefinition.model_validate(value)

        combined = definition.source.combined_distinct_tables[0]
        self.assertEqual(combined.output_dataset, "uoms")
        self.assertEqual(len(combined.inputs), 2)

    def test_combined_distinct_source_rejects_unsafe_formula(self) -> None:
        value = _definition()
        source = dict(value["source"])
        source["combined_distinct_tables"] = [
            {
                "output_dataset": "uoms",
                "output_field": "UnitId",
                "formula": "__import__('os')",
                "inputs": [{"file": "uoms.csv", "field": "UnitId"}],
            }
        ]
        value["source"] = source

        with self.assertRaisesRegex(ValidationError, "unknown value"):
            ScenarioDefinition.model_validate(value)

    def test_read_only_scenario_cannot_request_reconciliation(self) -> None:
        value = _definition()
        value["execution"] = {
            "stop_after": "RECONCILIATION",
            "write_policy": "READ_ONLY",
        }

        with self.assertRaisesRegex(
            ValidationError,
            "requires DISPOSABLE_SCENARIO_ONLY",
        ):
            ScenarioDefinition.model_validate(value)

    def test_remote_scenario_requires_pinned_target_identity(self) -> None:
        value = _definition()
        destination = dict(value["destination"])
        destination["mode"] = "REMOTE_ODOO"
        value["destination"] = destination

        with self.assertRaisesRegex(ValidationError, "expected_target_hash"):
            ScenarioDefinition.model_validate(value)

        destination["expected_target_hash"] = "sha256:" + "4" * 64
        definition = ScenarioDefinition.model_validate(value)

        self.assertEqual(
            definition.destination.expected_target_hash,
            "sha256:" + "4" * 64,
        )

    def test_write_scenario_requires_independent_target_projection(self) -> None:
        value = _definition()
        expectations = dict(value["expectations"])
        expectations.pop("target_projection")
        expectations.pop("target_projection_hash")
        value["expectations"] = expectations

        with self.assertRaisesRegex(ValidationError, "target projection"):
            ScenarioDefinition.model_validate(value)

    def test_comparison_must_account_for_all_prepared_rows(self) -> None:
        value = _definition()
        expectations = dict(value["expectations"])
        first = dict(expectations["first_comparison"])
        first["create"] = 2
        expectations["first_comparison"] = first
        value["expectations"] = expectations

        with self.assertRaisesRegex(ValidationError, "every prepared row"):
            ScenarioDefinition.model_validate(value)

    def test_target_projection_rejects_odoo_id_fields(self) -> None:
        with self.assertRaisesRegex(ValidationError, "projection record is invalid"):
            TargetProjection.model_validate(
                {
                    "contract_version": 1,
                    "records": [
                        {
                            "model": "res.partner",
                            "identity": {"id": 42},
                            "values": {"name": "Example"},
                        }
                    ],
                }
            )

    def test_target_projection_requires_unique_business_identities(self) -> None:
        record = {
            "model": "res.partner",
            "identity": {"ref": "CONTACT-001"},
            "values": {"name": "Example"},
        }

        with self.assertRaisesRegex(ValidationError, "identities must be unique"):
            TargetProjection.model_validate(
                {"contract_version": 1, "records": [record, record]}
            )

    def test_target_projection_accepts_business_relationship_reference(self) -> None:
        projection = TargetProjection.model_validate(
            {
                "contract_version": 1,
                "records": [
                    {
                        "model": "mrp.bom.line",
                        "identity": {"sequence": 10},
                        "values": {"product_qty": 2950.0},
                        "relationships": {
                            "product_id": {
                                "model": "product.product",
                                "identity": {"default_code": "COMPONENT-001"},
                            }
                        },
                    }
                ],
            }
        )

        self.assertEqual(
            projection.records[0].relationships["product_id"].model,
            "product.product",
        )

    def test_target_projection_relationship_rejects_database_id(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "projection reference is invalid",
        ):
            TargetProjection.model_validate(
                {
                    "contract_version": 1,
                    "records": [
                        {
                            "model": "mrp.bom.line",
                            "identity": {"sequence": 10},
                            "values": {"product_qty": 1.0},
                            "relationships": {
                                "product_id": {
                                    "model": "product.product",
                                    "identity": {"id": 42},
                                }
                            },
                        }
                    ],
                }
            )

    def test_expected_block_is_read_only_and_contains_blockers(self) -> None:
        value = _definition()
        value["execution"] = {
            "stop_after": "FIRST_COMPARISON",
            "write_policy": "READ_ONLY",
        }
        value["expectations"] = {
            "expected_outcome": "EXPECTED_BLOCK",
            "prepared_rows": 3,
            "first_comparison": {
                "create": 2,
                "update": 0,
                "unchanged": 0,
                "blocked": 1,
                "ambiguous": 0,
            },
        }

        definition = ScenarioDefinition.model_validate(value)

        self.assertEqual(definition.expectations.expected_outcome, "EXPECTED_BLOCK")

    def test_odoo_capture_requires_explicit_bounded_models(self) -> None:
        value = _definition()
        value["source"] = {
            "mode": "ODOO",
            "source_profile": "source.acceptance",
            "root_models": ["product.template"],
            "models": [
                {
                    "model": "product.template",
                    "fields": ["default_code", "name"],
                    "maximum_rows": 100,
                    "relationships": [
                        {
                            "field": "categ_id",
                            "target_model": "product.category",
                            "kind": "many2one",
                            "identity_fields": ["name"],
                            "required_for_migration": True,
                        }
                    ],
                },
                {
                    "model": "product.category",
                    "fields": ["name"],
                    "maximum_rows": 20,
                },
            ],
            "maximum_total_records": 120,
            "maximum_depth": 1,
            "allowed_company_keys": ["main-company"],
        }

        definition = ScenarioDefinition.model_validate(value)

        self.assertEqual(definition.source.mode, "ODOO")
        self.assertEqual(len(definition.source.models), 2)


if __name__ == "__main__":
    unittest.main()
