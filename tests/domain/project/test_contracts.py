"""Freeze the current Migration Project and multi-Recipe architecture."""

from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from copy import deepcopy
import json
from pathlib import Path
import re
import unittest
from uuid import UUID

from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import assert_no_numeric_odoo_ids


ROOT = REPOSITORY_ROOT
FIXTURE = (
    ROOT
    / "fixtures"
    / "migration-projects"
    / "current-contract"
    / "acceptance-contract.json"
)
RETAINED_RECIPE_FIXTURE = (
    ROOT
    / "fixtures"
    / "migration-projects"
    / "current-contract"
    / "customer-recipe-v1.json"
)
HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _by(items: list[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    return {str(item[key]): item for item in items}


def _recipe_revision(
    recipes: dict[str, dict[str, object]],
    recipe_id: str,
    version: int,
) -> dict[str, object] | None:
    recipe = recipes.get(recipe_id)
    if recipe is None:
        return None
    return next(
        (
            revision
            for revision in recipe["revisions"]
            if int(revision["version"]) == version
        ),
        None,
    )


def _selected_key(item: dict[str, object]) -> tuple[str, int, str]:
    return (
        str(item["recipe_id"]),
        int(item["recipe_revision"]),
        str(item["semantic_hash"]),
    )


def _dependency_has_cycle(
    recipe_ids: set[str],
    edges: list[dict[str, object]],
) -> bool:
    following = {recipe_id: set() for recipe_id in recipe_ids}
    indegree = {recipe_id: 0 for recipe_id in recipe_ids}
    for edge in edges:
        before = str(edge["before_recipe_id"])
        after = str(edge["after_recipe_id"])
        if before not in recipe_ids or after not in recipe_ids:
            return True
        if after not in following[before]:
            following[before].add(after)
            indegree[after] += 1
    ready = sorted(key for key, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for successor in sorted(following[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort()
    return visited != len(recipe_ids)


def _validate_contract(contract: dict[str, object]) -> set[str]:
    """Return deterministic issue codes for the frozen architecture fixture."""

    issues: set[str] = set()
    project = contract["project"]
    project_id = str(project["project_id"])
    data_versions = _by(contract["data_versions"], "data_version_id")
    recipes = _by(contract["recipes"], "recipe_id")
    runs = _by(contract["migration_runs"], "migration_run_id")
    bindings = _by(contract["target_bindings"], "target_binding_id")
    workspaces = _by(contract["workspaces"], "workspace_id")
    applications = _by(contract["recipe_applications"], "application_id")

    if set(project["data_version_ids"]) != set(data_versions):
        issues.add("PROJECT_DATA_VERSION_MEMBERSHIP_MISMATCH")
    if set(project["recipe_ids"]) != set(recipes):
        issues.add("PROJECT_RECIPE_MEMBERSHIP_MISMATCH")
    if set(project["migration_run_ids"]) != set(runs):
        issues.add("PROJECT_RUN_MEMBERSHIP_MISMATCH")

    for data_version in data_versions.values():
        if str(data_version["project_id"]) != project_id:
            issues.add("DATA_VERSION_PROJECT_MISMATCH")
        if any(
            key in data_version
            for key in (
                "recipe_id",
                "workspace_project_id",
                "pinned_recipe_revision",
            )
        ):
            issues.add("DATA_VERSION_LEGACY_OWNERSHIP")

    for recipe in recipes.values():
        if str(recipe["project_id"]) != project_id:
            issues.add("RECIPE_PROJECT_MISMATCH")
        for revision in recipe["revisions"]:
            if int(revision["recipe_contract_version"]) != 2:
                issues.add("RECIPE_ENVELOPE_VERSION_MISMATCH")
            if HASH.fullmatch(str(revision["semantic_hash"])) is None:
                issues.add("RECIPE_SEMANTIC_HASH_INVALID")

    for run in runs.values():
        run_id = str(run["migration_run_id"])
        if str(run["project_id"]) != project_id:
            issues.add("RUN_PROJECT_MISMATCH")
        if str(run["data_version_id"]) not in data_versions:
            issues.add("RUN_DATA_VERSION_MISMATCH")
        if str(run["target_binding_id"]) not in bindings:
            issues.add("RUN_TARGET_BINDING_MISSING")
        if set(run["workspace_ids"]) != {
            workspace_id
            for workspace_id, workspace in workspaces.items()
            if str(workspace["migration_run_id"]) == run_id
        }:
            issues.add("RUN_WORKSPACE_MEMBERSHIP_MISMATCH")
        if set(run["application_ids"]) != {
            application_id
            for application_id, application in applications.items()
            if str(application["migration_run_id"]) == run_id
        }:
            issues.add("RUN_APPLICATION_MEMBERSHIP_MISMATCH")

    for workspace in workspaces.values():
        run = runs.get(str(workspace["migration_run_id"]))
        if (
            run is None
            or str(workspace["project_id"]) != str(run["project_id"])
            or str(workspace["data_version_id"]) != str(run["data_version_id"])
        ):
            issues.add("WORKSPACE_RUN_CONTEXT_MISMATCH")
        application_id = workspace["recipe_application_id"]
        if application_id is not None:
            application = applications.get(str(application_id))
            if (
                application is None
                or str(application["workspace_id"]) != str(workspace["workspace_id"])
            ):
                issues.add("WORKSPACE_APPLICATION_MISMATCH")

    for application in applications.values():
        run = runs.get(str(application["migration_run_id"]))
        workspace = workspaces.get(str(application["workspace_id"]))
        revision = _recipe_revision(
            recipes,
            str(application["recipe_id"]),
            int(application["recipe_revision"]),
        )
        if (
            run is None
            or str(application["project_id"]) != str(run["project_id"])
            or str(application["data_version_id"]) != str(run["data_version_id"])
        ):
            issues.add("APPLICATION_RUN_CONTEXT_MISMATCH")
        if run is not None and str(application["target_binding_id"]) != str(
            run["target_binding_id"]
        ):
            issues.add("RUN_TARGET_MISMATCH")
        if (
            workspace is None
            or str(workspace["recipe_application_id"])
            != str(application["application_id"])
        ):
            issues.add("APPLICATION_WORKSPACE_MISMATCH")
        if revision is None or str(revision["semantic_hash"]) != str(
            application["recipe_semantic_hash"]
        ):
            issues.add("APPLICATION_RECIPE_REVISION_MISMATCH")

    plan = contract["cutover_plan"]
    if str(plan["project_id"]) != project_id:
        issues.add("CUTOVER_PLAN_PROJECT_MISMATCH")
    plan_revision = next(
        revision
        for revision in plan["revisions"]
        if int(revision["version"]) == int(plan["current_revision"])
    )
    selected = {
        str(item["recipe_id"]): item
        for item in plan_revision["selected_recipe_revisions"]
    }
    if set(selected) != set(project["recipe_ids"]):
        issues.add("CUTOVER_RECIPE_SELECTION_MISMATCH")
    for item in selected.values():
        revision = _recipe_revision(
            recipes,
            str(item["recipe_id"]),
            int(item["recipe_revision"]),
        )
        if revision is None or str(revision["semantic_hash"]) != str(
            item["semantic_hash"]
        ):
            issues.add("CUTOVER_RECIPE_SELECTION_MISMATCH")

    selected_ids = set(selected)
    if _dependency_has_cycle(selected_ids, plan_revision["dependency_edges"]):
        issues.add("CUTOVER_DEPENDENCY_CYCLE")

    write_owner: dict[tuple[str, str], str] = {}
    for declaration in plan_revision["write_ownership"]:
        recipe_id = str(declaration["recipe_id"])
        if recipe_id not in selected_ids:
            issues.add("CUTOVER_WRITE_OWNER_NOT_SELECTED")
        for field in declaration["fields"]:
            write_key = (str(declaration["model"]), str(field))
            previous = write_owner.setdefault(write_key, recipe_id)
            if previous != recipe_id:
                issues.add("CUTOVER_WRITE_COLLISION")

    qualification = contract["cutover_plan_qualification"]
    test_run = runs.get(str(qualification["test_run_id"]))
    qualification_selected = {
        _selected_key(item) for item in qualification["selected_recipe_revisions"]
    }
    plan_selected = {
        _selected_key(item) for item in plan_revision["selected_recipe_revisions"]
    }
    qualified_application_ids = set(qualification["application_ids"])
    if (
        str(qualification["project_id"]) != project_id
        or str(qualification["cutover_plan_id"]) != str(plan["cutover_plan_id"])
        or int(qualification["cutover_plan_revision"])
        != int(plan_revision["version"])
        or test_run is None
        or str(test_run["purpose"]) != "TEST"
        or qualified_application_ids != set(test_run["application_ids"])
        or qualification_selected != plan_selected
    ):
        issues.add("CUTOVER_QUALIFICATION_MISMATCH")
    else:
        qualified_applications = [
            applications[application_id]
            for application_id in qualified_application_ids
        ]
        application_selected = {
            (
                str(application["recipe_id"]),
                int(application["recipe_revision"]),
                str(application["recipe_semantic_hash"]),
            )
            for application in qualified_applications
        }
        if application_selected != plan_selected or any(
            application["status"] != "RECONCILED"
            for application in qualified_applications
        ):
            issues.add("CUTOVER_QUALIFICATION_MISMATCH")

    selection = contract["project_cutover_selection"]
    if (
        str(selection["project_id"]) != project_id
        or str(selection["cutover_plan_id"]) != str(plan["cutover_plan_id"])
        or int(selection["cutover_plan_revision"]) != int(plan_revision["version"])
        or str(selection["qualification_id"])
        != str(qualification["qualification_id"])
    ):
        issues.add("PROJECT_CUTOVER_SELECTION_MISMATCH")
    if any(
        key in selection
        for key in (
            "production_run_id",
            "data_version_id",
            "target_binding_id",
            "credential_generation",
        )
    ):
        issues.add("CUTOVER_SELECTION_CONTAINS_PRODUCTION_CONTEXT")
    if bool(selection["grants_write_authority"]):
        issues.add("CUTOVER_SELECTION_GRANTED_WRITE_AUTHORITY")
    production_runs = [
        run for run in runs.values() if str(run["purpose"]) == "PRODUCTION"
    ]
    if len(production_runs) != 1 or str(
        production_runs[0]["cutover_selection_id"]
    ) != str(selection["cutover_selection_id"]):
        issues.add("PRODUCTION_RUN_SELECTION_MISMATCH")

    return issues


def _mutate(contract: dict[str, object], mutation: str) -> None:
    if mutation == "CHANGE_CUSTOMER_RECIPE_PROJECT":
        contract["recipes"][0]["project_id"] = (
            "10000000-0000-4000-8000-000000000010"
        )
        return
    if mutation == "CHANGE_ONE_TEST_APPLICATION_TARGET":
        contract["recipe_applications"][0]["target_binding_id"] = (
            "90000000-0000-4000-8000-000000000003"
        )
        return
    if mutation == "ADD_PRODUCTION_RUN_TO_SELECTION":
        contract["project_cutover_selection"]["production_run_id"] = (
            "30000000-0000-4000-8000-000000000003"
        )
        return
    plan_revision = contract["cutover_plan"]["revisions"][0]
    if mutation == "ADD_REVERSE_DEPENDENCY_EDGE":
        plan_revision["dependency_edges"].append(
            {
                "before_recipe_id": "50000000-0000-4000-8000-000000000002",
                "after_recipe_id": "50000000-0000-4000-8000-000000000001",
                "kind": "PROJECT_SEQUENCE",
                "reason": "Rejected reverse edge",
            }
        )
        return
    if mutation == "ADD_PRODUCT_RECIPE_PARTNER_NAME_WRITE":
        plan_revision["write_ownership"].append(
            {
                "recipe_id": "50000000-0000-4000-8000-000000000002",
                "model": "res.partner",
                "fields": ["name"],
            }
        )
        return
    if mutation == "CHANGE_QUALIFIED_PRODUCT_RECIPE_REVISION":
        contract["cutover_plan_qualification"]["selected_recipe_revisions"][1][
            "recipe_revision"
        ] = 1
        return
    raise AssertionError(f"Unknown fixture mutation: {mutation}")


class MigrationProjectContractTests(unittest.TestCase):
    """Protect the implemented Project ownership model."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _load(FIXTURE)
        cls.retained_recipe = _load(RETAINED_RECIPE_FIXTURE)

    def test_fixture_is_current_and_internally_valid(self) -> None:
        self.assertEqual(self.contract["contract_version"], 1)
        self.assertEqual(self.contract["contract_status"], "CURRENT")
        self.assertEqual(_validate_contract(self.contract), set())

    def test_project_supports_zero_one_or_several_recipes(self) -> None:
        examples = {
            item["case"]: item for item in self.contract["cardinality_examples"]
        }
        self.assertEqual(
            set(examples),
            {"ZERO_RECIPES", "ONE_RECIPE", "SEVERAL_RECIPES"},
        )
        self.assertEqual(len(examples["ZERO_RECIPES"]["recipe_ids"]), 0)
        self.assertEqual(len(examples["ONE_RECIPE"]["recipe_ids"]), 1)
        self.assertGreaterEqual(
            len(examples["SEVERAL_RECIPES"]["recipe_ids"]),
            2,
        )
        self.assertEqual(
            examples["SEVERAL_RECIPES"]["project_id"],
            self.contract["project"]["project_id"],
        )

    def test_aggregate_identity_namespaces_are_distinct(self) -> None:
        identity_groups = {
            "project": [self.contract["project"]["project_id"]],
            "data_version": [
                item["data_version_id"] for item in self.contract["data_versions"]
            ],
            "migration_run": [
                item["migration_run_id"]
                for item in self.contract["migration_runs"]
            ],
            "workspace": [
                item["workspace_id"] for item in self.contract["workspaces"]
            ],
            "recipe": [item["recipe_id"] for item in self.contract["recipes"]],
            "application": [
                item["application_id"]
                for item in self.contract["recipe_applications"]
            ],
            "cutover_plan": [
                self.contract["cutover_plan"]["cutover_plan_id"]
            ],
            "qualification": [
                self.contract["cutover_plan_qualification"]["qualification_id"]
            ],
            "cutover_selection": [
                self.contract["project_cutover_selection"]["cutover_selection_id"]
            ],
            "target_binding": [
                item["target_binding_id"]
                for item in self.contract["target_bindings"]
            ],
        }
        defined: dict[str, str] = {}
        for namespace, values in identity_groups.items():
            for value in values:
                self.assertEqual(str(UUID(str(value))), value)
                self.assertNotIn(value, defined)
                defined[str(value)] = namespace

    def test_data_versions_are_project_owned_complete_packages(self) -> None:
        data_versions = self.contract["data_versions"]
        self.assertEqual(
            [item["purpose"] for item in data_versions],
            ["AUTHORING", "TEST", "PRODUCTION"],
        )
        self.assertTrue(all(item["state"] == "FROZEN" for item in data_versions))
        self.assertEqual(
            data_versions[-1]["label"],
            "Rollout export - 31 August, 18:00 cutoff",
        )
        self.assertEqual(
            [set(item["logical_dataset_ids"]) for item in data_versions],
            [set(data_versions[0]["logical_dataset_ids"])] * 3,
        )
        self.assertEqual(
            len({item["source_package_hash"] for item in data_versions}),
            3,
        )
        for item in data_versions:
            self.assertEqual(
                item["project_id"],
                self.contract["project"]["project_id"],
            )
            for forbidden in (
                "recipe_id",
                "workspace_project_id",
                "pinned_recipe_revision",
            ):
                self.assertNotIn(forbidden, item)

    def test_retained_recipe_semantic_envelope_remains_portable(self) -> None:
        frozen = self.contract["retained_recipe_envelope"]
        self.assertEqual(
            set(self.retained_recipe),
            set(frozen["envelope_fields"]),
        )
        self.assertEqual(
            self.retained_recipe["recipe_contract_version"],
            frozen["recipe_contract_version"],
        )
        semantic = self.retained_recipe["recipe"]
        self.assertEqual(set(semantic), set(frozen["semantic_fields"]))
        self.assertEqual(
            self.retained_recipe["semantic_hash"],
            content_hash(semantic),
        )
        semantic_keys = set(_keys(semantic))
        self.assertTrue(
            set(frozen["forbidden_semantic_fields"]).isdisjoint(semantic_keys)
        )
        assert_no_numeric_odoo_ids(semantic)

    def test_one_run_owns_one_target_and_isolates_application_workspaces(self) -> None:
        runs = _by(self.contract["migration_runs"], "migration_run_id")
        applications = self.contract["recipe_applications"]
        workspaces = self.contract["workspaces"]
        for application in applications:
            run = runs[application["migration_run_id"]]
            self.assertEqual(
                application["target_binding_id"],
                run["target_binding_id"],
            )
        application_workspaces = [
            item for item in workspaces if item["recipe_application_id"] is not None
        ]
        self.assertEqual(len(application_workspaces), len(applications))
        self.assertEqual(
            len({item["workspace_id"] for item in application_workspaces}),
            len(applications),
        )
        self.assertTrue(
            all(
                "credential_generation" not in application
                for application in applications
            )
        )

    def test_cutover_plan_pins_a_dag_without_write_collisions(self) -> None:
        plan_revision = self.contract["cutover_plan"]["revisions"][0]
        selected = {
            item["recipe_id"] for item in plan_revision["selected_recipe_revisions"]
        }
        self.assertFalse(
            _dependency_has_cycle(selected, plan_revision["dependency_edges"])
        )
        owners: dict[tuple[str, str], str] = {}
        for declaration in plan_revision["write_ownership"]:
            for field in declaration["fields"]:
                key = (declaration["model"], field)
                self.assertNotIn(key, owners)
                owners[key] = declaration["recipe_id"]

    def test_qualification_pins_test_evidence_not_production_authority(self) -> None:
        qualification = self.contract["cutover_plan_qualification"]
        selection = self.contract["project_cutover_selection"]
        runs = _by(self.contract["migration_runs"], "migration_run_id")
        applications = _by(
            self.contract["recipe_applications"],
            "application_id",
        )
        self.assertEqual(runs[qualification["test_run_id"]]["purpose"], "TEST")
        self.assertTrue(
            all(
                applications[application_id]["status"] == "RECONCILED"
                for application_id in qualification["application_ids"]
            )
        )
        self.assertEqual(
            set(selection),
            {
                "cutover_plan_id",
                "cutover_plan_revision",
                "cutover_selection_id",
                "grants_write_authority",
                "project_id",
                "qualification_id",
            },
        )
        self.assertFalse(selection["grants_write_authority"])
        production_run = next(
            run for run in runs.values() if run["purpose"] == "PRODUCTION"
        )
        self.assertEqual(
            production_run["cutover_selection_id"],
            selection["cutover_selection_id"],
        )
        production_applications = [
            applications[application_id]
            for application_id in production_run["application_ids"]
        ]
        self.assertTrue(
            all(
                item["status"] == "DRAFT_READINESS"
                for item in production_applications
            )
        )

    def test_out_of_scope_behaviors_are_exact(self) -> None:
        self.assertEqual(
            set(self.contract["out_of_scope"]),
            {
                "ARBITRARY_CROSS_RECIPE_MERGE",
                "CROSS_PROJECT_RECIPE_SHARING",
                "DELTA_OR_INFERRED_DELETE_SEMANTICS",
                "MIXED_TARGET_RUNS",
                "UNATTENDED_ROLLOUT",
            },
        )

    def test_rejected_cases_fail_closed_with_the_expected_issue(self) -> None:
        for case in self.contract["rejected_cases"]:
            with self.subTest(case=case["case"]):
                changed = deepcopy(self.contract)
                _mutate(changed, str(case["mutation"]))
                self.assertIn(
                    case["expected_issue_code"],
                    _validate_contract(changed),
                )


if __name__ == "__main__":
    unittest.main()

