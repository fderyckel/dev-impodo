"""Manual setup resumes only a verified, owned M0 allocation."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from impodo.domain.shared.models import TargetFingerprint, TargetRecord
from scripts import prepare_practice_manufacturing_target as setup
from tests.support.paths import REPOSITORY_ROOT

ASSETS = REPOSITORY_ROOT / "scenarios/impodo-practice/manufacturing/v1"


def initial_target(major, oracle, spec):
    """Declare M0 independently, with all learner-created category paths."""
    companies = {"NORTH": 1, "SOUTH": 2}
    data = {
        "res.company": [
            {
                "id": 1,
                "name": "Morrow Assembly North",
                "partner_id": 101,
                "user_ids": [5],
            },
            {
                "id": 2,
                "name": "Morrow Assembly South",
                "partner_id": 102,
                "user_ids": [5],
            },
        ],
        "product.category": [],
        "product.tag": [{"id": 21, "name": "MORROW"}, {"id": 22, "name": "TRAINING"}],
        "uom.uom": [{"id": 1, "name": "Units"}],
        "mrp.workcenter": [],
        "resource.resource": [],
        "product.template": [],
        "product.product": [],
        "mrp.bom": [],
    }
    categories = {}
    for identifier, row in enumerate(oracle["baseline"]["categories"], 31):
        path = tuple(row["path"])
        categories[path] = identifier
        values = {
            "id": identifier,
            "name": path[-1],
            "parent_id": categories[path[:-1]] if len(path) > 1 else False,
        }
        if major == 20:
            values["company_id"] = False
        data["product.category"].append(values)
    for identifier, row in enumerate(spec["work_centres"], 61):
        values = {
            "name": row["name"],
            "company_id": companies[row["company_code"]],
            "active": True,
        }
        data["mrp.workcenter"].append(
            {
                "id": identifier,
                "code": row["code"],
                "resource_id": identifier + 10,
                **values,
            }
        )
        data["resource.resource"].append({"id": identifier + 10, **values})
    reference = categories[("Services",)]
    data["ir.model.data"] = [
        {
            "id": 51,
            "module": "product",
            "name": "product_category_services",
            "model": "product.category",
            "res_id": reference,
            "noupdate": True,
        }
    ]
    owned = {}
    for model, identifiers, origin in (
        ("res.company", [2], "explicit_seed_create"),
        (
            "product.category",
            [categories[("Components",)], categories[("Components", "Metals")]],
            "explicit_seed_create",
        ),
        ("product.tag", [21, 22], "explicit_seed_create"),
        ("mrp.workcenter", list(range(61, 65)), "explicit_seed_create"),
        ("res.partner", [102], "owned_company_generated_partner"),
        (
            "resource.resource",
            list(range(71, 75)),
            "owned_workcentre_generated_resource",
        ),
    ):
        owned[model] = [
            {"id": identifier, "origin": origin} for identifier in identifiers
        ]
    operations = [
        {
            "kind": "write",
            "model": "res.company",
            "id": 1,
            "values": {"name": "Morrow Assembly North"},
            "status": "ACKNOWLEDGED",
        }
    ]
    operations.append(
        {
            "kind": "create",
            "model": "res.company",
            "id": None,
            "values": {"name": "Morrow Assembly South", "user_ids": [[4, 5]]},
            "receipt_ids": [2],
            "status": "ACKNOWLEDGED",
        }
    )
    for path in [("Components",), ("Components", "Metals")]:
        values = {
            "name": path[-1],
            "parent_id": categories[path[:-1]] if len(path) > 1 else False,
        }
        if major == 20:
            values["company_id"] = False
        operations.append(
            {
                "kind": "create",
                "model": "product.category",
                "id": None,
                "values": values,
                "receipt_ids": [categories[path]],
                "status": "ACKNOWLEDGED",
            }
        )
    for model, fields in [
        ("product.tag", ("name",)),
        ("mrp.workcenter", ("code", "name", "company_id")),
    ]:
        for row in data[model]:
            operations.append(
                {
                    "kind": "create",
                    "model": model,
                    "id": None,
                    "values": {field: row[field] for field in fields},
                    "receipt_ids": [row["id"]],
                    "status": "ACKNOWLEDGED",
                }
            )
    ledger = {
        "contract_version": 1,
        "purpose": "morrow_practice_qualification",
        "major": major,
        "database": f"impodo_scenario_morrow{major}_manual_test",
        "state": "M0_SEEDED",
        "initial_company": {"id": 1, "values": {"name": "Original Company"}},
        "references": {
            "product.category": [
                {
                    "id": reference,
                    "source_key": "CAT-10",
                    "path": ["Services"],
                    "xmlid": "product.product_category_services",
                    "noupdate": True,
                    "owned": False,
                }
            ]
        },
        "owned": owned,
        "operations": operations,
    }
    return data, ledger


class ReadOnlyIdentity:
    def __init__(self, major):
        self.major = major

    def get_target_fingerprint(self):
        return TargetFingerprint(
            "manual-target",
            "LOCAL",
            f"impodo_scenario_morrow{self.major}_manual_test",
            f"{self.major}.0",
            "2026-11-06T12:00:00Z",
            dict.fromkeys(("base", "product", "stock", "mrp"), "1"),
        )

    def probe_read_identity(self, models):
        if "ir.model.data" not in models:
            raise AssertionError("Native reference identity was not checked")

    def _post_read_method(self, model, method, payload):
        if (model, method) != ("res.users", "context_get"):
            raise AssertionError("Unexpected identity method")
        return {"uid": 5}


class WriteIdentity:
    def __init__(self, config):
        pass

    def probe_write_identity(self, *args, **kwargs):
        if "mrp.bom" not in kwargs["creatable_models"]:
            raise AssertionError("Manufacturing write authority was not checked")


def seed_type(major, data, oracle, spec):
    class Seed:
        writes = 0

        def __init__(self, descriptor, root, private):
            self.major, self.oracle, self.seed_spec = major, oracle, spec
            self.reader, self.write_config = ReadOnlyIdentity(major), None
            self.private = Path(private)
            self.private.mkdir(mode=0o700)
            self.ledger_path = self.private / "owned-records.json"
            self.ledger = {"state": "READ_ONLY", "owned": {}}
            setup.private_json(self.ledger_path, self.ledger)

        def records(self, model, fields, domain, *, company_id=None, limit=100):
            def matches(row, term):
                field, operator, value = term
                actual = row.get(field, False)
                if operator == "=":
                    return actual == value
                if operator == "in":
                    return actual in value
                if operator == ">":
                    return actual > value
                if operator == "like":
                    return isinstance(actual, str) and actual.startswith(
                        value.removesuffix("%")
                    )
                raise AssertionError(operator)

            return tuple(
                TargetRecord(model, row["id"], {field: row[field] for field in fields})
                for row in data[model]
                if all(matches(row, term) for term in domain)
                and (
                    not company_id
                    or not row.get("company_id")
                    or row["company_id"] == company_id
                )
            )[:limit]

        def seed_existing_products(self, *, allow_write=False):
            if allow_write is not True or self.ledger["state"] != "M0_SEEDED":
                raise AssertionError("Closed seed authority was not retained")
            type(self).writes += 1
            self.ledger["state"] = "M2_SEEDED"
            setup.private_json(self.ledger_path, self.ledger)
            return {"products": 8, "variants": 8, "categories": 12}

    return Seed


def private_stat_scope(directory, *, readable=False):
    """Model private mode bits portably; preserve native file types and I/O."""
    original = Path.stat

    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == directory or directory in path.parents:
            mode = result.st_mode & ~0o077
            if readable:
                mode |= 0o004
            return os.stat_result((mode, *result[1:]))
        return result

    return patch.object(Path, "stat", autospec=True, side_effect=stat)


class ManualManufacturingSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oracle = json.loads((ASSETS / "expected-business-results.json").read_text())
        cls.spec = json.loads((ASSETS / "target-seed-projection.json").read_text())

    def check_m0(self, data, ledger, major):
        Seed = seed_type(major, data, self.oracle, self.spec)
        seed = Seed.__new__(Seed)
        seed.major, seed.oracle, seed.seed_spec = major, self.oracle, self.spec
        return setup.check_owned_m0(seed, ledger, 5)

    def test_exact_ownership_and_altered_setup_both_majors(self):
        for major in (19, 20):
            data, ledger = initial_target(major, self.oracle, self.spec)
            with self.subTest(major=major, case="complete"):
                counts, unit = self.check_m0(data, ledger, major)
                self.assertEqual(counts["categories"], 12)
                self.assertEqual(counts["owned_resources"], 4)
                self.assertEqual(unit, 1)
            faults = []
            altered = copy.deepcopy(ledger)
            altered["owned"]["product.category"][0]["id"] = 999
            faults.append((data, altered, "M0_CATEGORY_OWNERSHIP_DIFFERED"))
            altered = copy.deepcopy(ledger)
            altered["operations"][0]["status"] = "DISPATCHING"
            faults.append((data, altered, "ACKNOWLEDGED_M0_LEDGER_REQUIRED"))
            altered = copy.deepcopy(ledger)
            altered["operations"][-1]["receipt_ids"] = [999]
            faults.append((data, altered, "SAVED_M0_OPERATIONS_DIFFERED"))
            altered = copy.deepcopy(data)
            altered["res.company"][1]["user_ids"] = []
            faults.append((altered, ledger, "SOUTH_PRINCIPAL_GRANT_MISSING"))
            altered = copy.deepcopy(data)
            altered["resource.resource"][0]["name"] = "wrong"
            faults.append((altered, ledger, "OWNED_RESOURCE_VALUES_DIFFERED"))
            altered = copy.deepcopy(data)
            altered["ir.model.data"][0]["noupdate"] = False
            faults.append((altered, ledger, "NATIVE_REFERENCE_DIFFERED"))
            altered = copy.deepcopy(data)
            altered["product.category"].pop()
            faults.append((altered, ledger, "CATEGORY_PATHS_DIFFERED"))
            altered = copy.deepcopy(data)
            altered["product.template"] = [{"id": 999, "default_code": "MA-P-0009"}]
            faults.append((altered, ledger, "FRESH_MORROW_PRODUCT_SCOPE_REQUIRED"))
            altered = copy.deepcopy(data)
            altered["mrp.workcenter"].append(
                {"id": 999, "code": "MA-WC-99", "company_id": 1}
            )
            faults.append((altered, ledger, "M0_WORKCENTRE_NAMESPACE_DIFFERED"))
            for current, saved, code in faults:
                with (
                    self.subTest(major=major, case=code),
                    self.assertRaisesRegex(RuntimeError, f"^{code}$"),
                ):
                    self.check_m0(current, saved, major)

    def test_guarded_resume_preserves_ledger_and_dispatches_once(self):
        for major in (19, 20):
            data, ledger = initial_target(major, self.oracle, self.spec)
            Seed = seed_type(major, data, self.oracle, self.spec)
            with self.subTest(major=major), tempfile.TemporaryDirectory() as scratch:
                directory, descriptor = (
                    Path(scratch) / "seed",
                    Path(scratch) / "connection.json",
                )
                directory.mkdir(mode=0o700)
                descriptor.write_text("{}")
                setup.private_json(directory / "owned-records.json", ledger)
                binding = {
                    "contract_version": 1,
                    "purpose": "morrow_manual_manufacturing_setup",
                    "descriptor_hash": setup.digest(descriptor),
                    "corpus": setup.corpus_binding(ASSETS),
                    "major": major,
                    "database": ledger["database"],
                    "principal_id": 5,
                    "target_hash": "manual-target",
                    "m0_ledger_hash": setup.digest(directory / "owned-records.json"),
                }
                setup.private_json(directory / setup.BINDING_FILE, binding)
                with (
                    private_stat_scope(directory),
                    patch.object(setup, "ManufacturingSeedClient", Seed),
                    patch.object(
                        setup,
                        "Json2ReadConnector",
                        lambda config, current_major=major: ReadOnlyIdentity(
                            current_major
                        ),
                    ),
                    patch.object(setup, "Json2WriteIdentityConnector", WriteIdentity),
                ):
                    args = SimpleNamespace(
                        allow_write=True,
                        expected_major=major,
                        target_descriptor=descriptor,
                        seed_directory=directory,
                        stage="existing-products",
                    )
                    result = setup.prepare(args, corpus_root=ASSETS)
                    self.assertEqual(result["counts"]["products"], 8)
                    self.assertFalse(result["migration_loaded"])
                    self.assertEqual(Seed.writes, 1)
                    self.assertEqual(
                        setup.read_private(directory / "owned-records.json")["state"],
                        "M2_SEEDED",
                    )
                    self.assertEqual(
                        setup.read_private(directory / "post-category-dispatch.json")[
                            "state"
                        ],
                        "ACKNOWLEDGED",
                    )
                    with self.assertRaises(RuntimeError):
                        setup.prepare(args, corpus_root=ASSETS)
                    self.assertEqual(Seed.writes, 1)

    def test_authorization_precedes_any_input_or_target_access(self):
        with tempfile.TemporaryDirectory() as scratch:
            missing = Path(scratch) / "absent"
            for stage in ("initial", "existing-products"):
                args = SimpleNamespace(
                    allow_write=False,
                    expected_major=20,
                    target_descriptor=missing / "connection.json",
                    seed_directory=missing / "seed",
                    stage=stage,
                )
                with (
                    self.subTest(stage=stage),
                    patch.object(setup, "ManufacturingSeedClient") as constructor,
                    self.assertRaisesRegex(RuntimeError, "^ALLOW_WRITE_REQUIRED$"),
                ):
                    setup.prepare(args, corpus_root=missing / "corpus")
                constructor.assert_not_called()
                self.assertFalse(missing.exists())

    def test_private_file_guard_rejects_shared_mode_bits(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            path = directory / "ledger.json"
            setup.private_json(path, {"private": True})
            with private_stat_scope(directory):
                self.assertEqual(setup.read_private(path), {"private": True})
            with (
                private_stat_scope(directory, readable=True),
                self.assertRaisesRegex(
                    RuntimeError, "^PRIVATE_FILE_PERMISSIONS_REQUIRED$"
                ),
            ):
                setup.read_private(path)
