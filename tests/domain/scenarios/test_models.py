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
