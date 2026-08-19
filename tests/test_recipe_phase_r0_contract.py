"""Freeze the Recipe-first Phase R0 contracts and rollout example."""

from __future__ import annotations

from copy import deepcopy
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import unittest

from impodo.domain.serialization import canonical_json, content_hash
from impodo.models import assert_no_numeric_odoo_ids


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "recipes" / "phase-r0"
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


def _sha256_file(name: str) -> str:
    return "sha256:" + hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()


def _without_hash(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != "content_hash"}


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


class RecipePhaseR0ContractTests(unittest.TestCase):
    """Protect the Recipe-first identity and Test-to-Production boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.envelope = _load_json("customer-recipe-v3.json")
        cls.acceptance = _load_json("acceptance-contract.json")

    def test_recipe_hash_is_portable_and_canonical(self) -> None:
        self.assertEqual(self.envelope["recipe_contract_version"], 2)
        recipe = self.envelope["recipe"]
        self.assertEqual(
            set(recipe),
            {
                "contract_versions",
                "source_shape",
                "parameter_definitions",
                "source_preparation",
                "mapping",
                "odoo_target_contract",
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

        changed = deepcopy(self.envelope)
        changed["provenance"]["origin_data_version_id"] = (
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        )
        changed["provenance"]["publisher"]["subject_id"] = "another-manager"
        changed["compatibility_hints"]["datasets"][0]["prior_display_name"] = (
            "Customers from another source file"
        )
        self.assertEqual(content_hash(changed["recipe"]), self.envelope["semantic_hash"])
        self.assertNotEqual(
            content_hash(
                {key: value for key, value in changed.items() if key != "payload_hash"}
            ),
            self.envelope["payload_hash"],
        )

        semantic_text = json.dumps(recipe, sort_keys=True)
        self.assertIsNone(UUID_TEXT.search(semantic_text))
        for forbidden in (
            "recipe_id",
            "data_version_id",
            "workspace_project_id",
            "project_id",
            "series_id",
            "mapping_id",
            "source_artifact",
            "source_artifact_hash",
            "endpoint",
            "database",
            "connection_target_hash",
            "credential_generation",
            "principal_hash",
            "permission_hash",
            "target_binding_id",
        ):
            self.assertNotIn(f'"{forbidden}"', semantic_text)
        assert_no_numeric_odoo_ids(recipe)

    def test_recipe_semantic_collections_have_deterministic_order(self) -> None:
        recipe = self.envelope["recipe"]
        columns = recipe["source_shape"]["datasets"][0]["columns"]
        self.assertEqual(
            [item["logical_column_id"] for item in columns],
            sorted(item["logical_column_id"] for item in columns),
        )
        parameters = recipe["parameter_definitions"]["parameters"]
        self.assertEqual(
            [item["logical_parameter_id"] for item in parameters],
            sorted(item["logical_parameter_id"] for item in parameters),
        )
        fields = recipe["mapping"]["datasets"][0]["fields"]
        self.assertEqual(
            [item["logical_field_id"] for item in fields],
            sorted(item["logical_field_id"] for item in fields),
        )
        dependencies = recipe["target_governance"]["dependencies"]
        self.assertEqual(
            [item["logical_dependency_id"] for item in dependencies],
            sorted(item["logical_dependency_id"] for item in dependencies),
        )

    def test_recipe_data_version_and_workspace_identities_are_independent(self) -> None:
        acceptance_text = json.dumps(self.acceptance, sort_keys=True)
        self.assertNotIn('"series_id"', acceptance_text)
        self.assertNotIn('"edition_number"', acceptance_text)

        recipe = self.acceptance["recipe"]
        data_versions = self.acceptance["data_versions"]
        data_version_ids = {item["data_version_id"] for item in data_versions}
        workspace_ids = {item["workspace_project_id"] for item in data_versions}
        self.assertEqual(len(data_version_ids), 2)
        self.assertEqual(len(workspace_ids), 2)
        self.assertTrue(data_version_ids.isdisjoint(workspace_ids))
        self.assertNotIn(recipe["recipe_id"], data_version_ids | workspace_ids)
        self.assertEqual(
            data_versions[1]["parent_data_version_id"],
            data_versions[0]["data_version_id"],
        )
        self.assertEqual(
            [(item["purpose"], item["state"]) for item in data_versions],
            [("TEST", "SEALED"), ("PRODUCTION", "ACTIVE")],
        )
        self.assertTrue(all(item["pinned_recipe_revision"] == 3 for item in data_versions))

        lineage = self.acceptance["recipe_revisions"]
        self.assertEqual([item["version"] for item in lineage], [1, 2, 3])
        self.assertEqual(
            [item["parent_version"] for item in lineage], [None, 1, 2]
        )
        self.assertEqual(
            lineage[-1]["semantic_hash"], self.envelope["semantic_hash"]
        )

        by_data_version = {
            item["data_version_id"]: item for item in data_versions
        }
        for application in self.acceptance["applications"]:
            data_version = by_data_version[application["data_version_id"]]
            self.assertEqual(
                application["workspace_project_id"],
                data_version["workspace_project_id"],
            )

    def test_each_data_version_has_fresh_source_parameters_and_controls(self) -> None:
        data_versions = self.acceptance["data_versions"]
        for data_version in data_versions:
            self.assertEqual(
                data_version["source_artifact_hash"],
                _sha256_file(data_version["source_artifact"]),
            )
            self.assertEqual(
                data_version["row_count"],
                len(_load_csv(data_version["source_artifact"])),
            )

        parameter_sets = self.acceptance["parameter_value_sets"]
        self.assertEqual(
            {item["data_version_id"] for item in parameter_sets},
            {item["data_version_id"] for item in data_versions},
        )
        for values in parameter_sets:
            self.assertEqual(values["content_hash"], content_hash(_without_hash(values)))
        self.assertNotEqual(parameter_sets[0]["content_hash"], parameter_sets[1]["content_hash"])

        expected = self.acceptance["expected_outcomes"]
        test_total = sum(
            Decimal(row["open_balance"])
            for row in _load_csv("customer-test-data.csv")
        )
        production_total = sum(
            Decimal(row["open_balance"])
            for row in _load_csv("customer-rollout-data.csv")
        )
        self.assertEqual(test_total, Decimal(expected["test"]["expected_open_balance"]))
        self.assertEqual(
            production_total,
            Decimal(expected["production"]["expected_open_balance"]),
        )
        control = self.envelope["recipe"]["control_definitions"]["controls"][0]
        self.assertFalse(control["invariant_expectation"])
        self.assertNotIn("expected_value", control)

    def test_target_bindings_are_distinct_current_and_secret_free(self) -> None:
        bindings = self.acceptance["target_bindings"]
        for binding in bindings:
            self.assertEqual(binding["content_hash"], content_hash(_without_hash(binding)))
            self.assertRegex(binding["connection_target_hash"], HASH)
            self.assertRegex(binding["content_hash"], HASH)

        test_binding, production_old, production_current, production_write = bindings
        self.assertNotEqual(
            test_binding["connection_target_hash"],
            production_old["connection_target_hash"],
        )
        self.assertNotEqual(test_binding["endpoint"], production_old["endpoint"])
        self.assertNotEqual(test_binding["database"], production_old["database"])
        self.assertEqual(
            production_old["connection_target_hash"],
            production_current["connection_target_hash"],
        )
        self.assertNotEqual(
            production_old["credential_generation"],
            production_current["credential_generation"],
        )
        self.assertEqual(production_current["credential_role"], "READ")
        self.assertEqual(production_write["credential_role"], "WRITE")
        self.assertNotEqual(
            production_current["target_binding_id"],
            production_write["target_binding_id"],
        )

        forbidden_secret_keys = {
            "api_key",
            "password",
            "secret",
            "secret_value",
            "access_token",
            "refresh_token",
            "token",
        }
        for value in _walk(self.acceptance):
            if isinstance(value, dict):
                self.assertTrue(forbidden_secret_keys.isdisjoint(value))

    def test_rotation_invalidates_target_evidence_not_recipe_or_source(self) -> None:
        applications = self.acceptance["applications"]
        for application in applications:
            self.assertEqual(
                application["content_hash"], content_hash(_without_hash(application))
            )
        test_application, production_stale, production_current = applications
        self.assertEqual(
            {
                item["recipe_semantic_hash"]
                for item in (test_application, production_stale, production_current)
            },
            {self.envelope["semantic_hash"]},
        )
        self.assertEqual(
            production_stale["source_selection_hash"],
            production_current["source_selection_hash"],
        )
        self.assertEqual(
            production_stale["source_artifact_hash"],
            production_current["source_artifact_hash"],
        )
        self.assertNotEqual(
            production_stale["target_binding_hash"],
            production_current["target_binding_hash"],
        )
        self.assertNotEqual(
            production_stale["comparison_hash"],
            production_current["comparison_hash"],
        )
        self.assertEqual(production_stale["status"], "APPLIED")
        self.assertEqual(production_current["status"], "APPLIED")
        readiness = {
            item["application_id"]: item
            for item in self.acceptance["application_readiness"]
        }
        self.assertEqual(
            readiness[production_stale["application_id"]],
            {
                "application_id": production_stale["application_id"],
                "state": "INVALIDATED",
                "reason": "CREDENTIAL_GENERATION_CHANGED",
                "invalidated_by_rotation_id": (
                    "99999999-9999-4999-8999-999999999999"
                ),
            },
        )
        self.assertEqual(
            readiness[production_current["application_id"]]["state"], "CURRENT"
        )

        rotation = self.acceptance["credential_rotation"]
        self.assertEqual(rotation["content_hash"], content_hash(_without_hash(rotation)))
        self.assertEqual(
            rotation["preserved_recipe_semantic_hash"],
            production_current["recipe_semantic_hash"],
        )
        self.assertEqual(
            rotation["preserved_source_selection_hash"],
            production_current["source_selection_hash"],
        )
        self.assertEqual(
            rotation["invalidated_evidence"], ["COMPARISON", "LOAD_READINESS"]
        )
        self.assertEqual(
            rotation["recovery_actions"],
            ["REPROBE_TARGET", "REFRESH_TARGET_EVIDENCE", "RECOMPARE"],
        )
        sequence = self.acceptance["production_rollout_sequence"]
        self.assertLess(
            sequence.index("PRODUCTION_COMPARISON_ACCEPTED"),
            sequence.index("READ_CREDENTIAL_ROTATED"),
        )
        self.assertLess(
            sequence.index("PRODUCTION_RECOMPARISON_ACCEPTED"),
            sequence.index("WRITE_BINDING_ESTABLISHED"),
        )

    def test_qualification_is_exact_test_evidence_not_production_authority(self) -> None:
        qualification = self.acceptance["qualification"]
        test_application = self.acceptance["applications"][0]
        test_binding = self.acceptance["target_bindings"][0]
        self.assertEqual(
            qualification["content_hash"], content_hash(_without_hash(qualification))
        )
        self.assertEqual(qualification["environment"], "TEST")
        self.assertEqual(
            qualification["application_evidence_hash"],
            test_application["content_hash"],
        )
        self.assertEqual(
            qualification["target_binding_hash"], test_binding["content_hash"]
        )
        self.assertEqual(qualification["status"], "TEST_QUALIFIED")

        candidate = self.acceptance["cutover_candidate"]
        self.assertEqual(candidate["content_hash"], content_hash(_without_hash(candidate)))
        self.assertEqual(
            candidate["qualification_evidence_hash"], qualification["content_hash"]
        )
        self.assertEqual(candidate["recipe_revision"], 3)
        for forbidden in (
            "endpoint",
            "database",
            "credential_generation",
            "target_binding_id",
            "data_version_id",
            "source_artifact_hash",
            "comparison_hash",
            "write_approval",
        ):
            self.assertNotIn(forbidden, candidate)

        production = self.acceptance["applications"][-1]
        self.assertNotEqual(
            production["target_binding_hash"], qualification["target_binding_hash"]
        )
        self.assertNotEqual(
            production["comparison_hash"], qualification["comparison_hash"]
        )

    def test_recipe_v3_covers_expected_rollout_transformations(self) -> None:
        mapping = self.envelope["recipe"]["mapping"]["datasets"][0]
        fields = {item["logical_field_id"]: item for item in mapping["fields"]}
        languages = {
            item["source_value"]: item["target_value"]
            for item in fields["field:customers.lang"]["value_matches"]
        }
        countries = {
            item["source_value"]: item["target_value"]
            for item in mapping["relationships"][0]["value_matches"]
        }
        self.assertEqual(
            languages,
            {"English": "en_US", "French": "fr_FR", "German": "de_DE"},
        )
        self.assertEqual(countries, {"BEL": "BE", "FRA": "FR", "LUX": "LU"})
        self.assertEqual(
            mapping["comparison_policy"]["missing_source_row"],
            "NO_DELETE_INFERENCE",
        )
        self.assertEqual(
            self.acceptance["expected_outcomes"]["production"]["inferred_deletes"],
            0,
        )
        self.assertEqual(
            self.acceptance["expected_outcomes"]["production"],
            {
                "source_rows": 4,
                "created": 2,
                "updated": 1,
                "unchanged": 1,
                "failed": 0,
                "inferred_deletes": 0,
                "expected_open_balance": "6000000.00",
                "reconciled_open_balance": "6000000.00",
            },
        )

    def test_intents_recovery_actions_and_bounds_are_frozen(self) -> None:
        intents = {
            item["kind"]: item for item in self.acceptance["intent_examples"]
        }
        self.assertEqual(
            set(intents),
            {
                "RECIPE_PUBLICATION",
                "DATA_VERSION_CREATION",
                "QUALIFICATION_PUBLICATION",
                "CUTOVER_SELECTION",
                "RECIPE_DELETION",
            },
        )
        self.assertEqual(intents["RECIPE_DELETION"]["state"], "TARGETS_ENUMERATED")
        self.assertEqual(len(intents["RECIPE_DELETION"]["targets"]), 5)
        for intent in intents.values():
            self.assertIn("recipe_id", intent)
            self.assertNotIn("series_id", intent)
            self.assertEqual(intent["retry_count"], 0)

        scenarios = {
            item["scenario"]: item
            for item in self.acceptance["recovery_scenarios"]
        }
        self.assertEqual(
            set(scenarios),
            {
                "credential_rotated_after_comparison",
                "production_schema_incompatible",
                "used_source_column_renamed",
                "new_categorical_value",
                "stale_cross_store_intent",
                "unknown_remote_write_outcome",
                "qualification_evidence_changed",
            },
        )
        self.assertEqual(
            scenarios["unknown_remote_write_outcome"]["recovery_action"],
            "RECONCILE_BEFORE_RETRY",
        )

        bounds = self.acceptance["bounds"]
        self.assertLessEqual(
            len(canonical_json(self.envelope).encode("utf-8")),
            bounds["recipe_payload_bytes"],
        )
        self.assertEqual(bounds["data_versions_per_recipe"], 1_000)
        self.assertEqual(bounds["recipe_revisions_per_recipe"], 1_000)
        self.assertEqual(bounds["intent_retries_before_escalation"], 20)
        self.assertEqual(
            self.acceptance["contract_versions"]["required_mapping_contract"],
            11,
        )


if __name__ == "__main__":
    unittest.main()
