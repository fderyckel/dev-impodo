"""Seed the two explicit setup stages of an allocated Morrow manufacturing lab.

The initial stage creates M0 only. After the learner loads all twelve category
paths through Impodo, existing-products validates the saved private M0 ledger
and current target before creating exactly eight initial templates. It never
loads a migration, starts services, retries, or cleans a target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from uuid import uuid4

from impodo.adapters.odoo.connectors import (
    Json2ReadConnector,
    Json2WriteIdentityConnector,
)
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from scripts.practice_customer_seed import NAMES, relation_id, require
from scripts.practice_manufacturing_projection import (
    MIGRATION_MODELS,
    READ_MODELS,
    SETUP_MODELS,
    WORKCENTRE_FIELDS,
    ManufacturingProjection,
    exact_index,
    relation_ids,
)
from scripts.practice_manufacturing_seed import ManufacturingSeedClient
from scripts.qualify_practice_manufacturing import validate_files

ROOT = (
    Path(__file__).resolve().parents[1] / "scenarios/impodo-practice/manufacturing/v1"
)
BINDING_FILE = "manual-seed-binding.json"


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_binding(root):
    return {
        name: digest(root / name)
        for name in (
            "manifest.json",
            "target-seed-projection.json",
            "expected-business-results.json",
        )
    }


def private_json(path, payload, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    os.chmod(path, 0o600)


def read_private(path):
    require(path.is_file() and not path.is_symlink(), "SAVED_PRIVATE_FILE_REQUIRED")
    require(path.stat().st_mode & 0o077 == 0, "PRIVATE_FILE_PERMISSIONS_REQUIRED")
    return json.loads(path.read_text(encoding="utf-8"))


def current_principal(seed):
    reader = seed.reader._post_read_method("res.users", "context_get", {"context": {}})
    writer = Json2ReadConnector(seed.write_config)._post_read_method(
        "res.users", "context_get", {"context": {}}
    )
    user = reader.get("uid")
    require(
        type(user) is int and user > 0 and writer.get("uid") == user,
        "SAME_MANUAL_SETUP_PRINCIPAL_REQUIRED",
    )
    return user


def check_runtime(seed, binding):
    fingerprint = seed.reader.get_target_fingerprint()
    observed = recognize_odoo_version(fingerprint.odoo_version)
    require(
        observed.major == seed.major
        and observed.series == f"{seed.major}.0"
        and observed.release_stage is OdooReleaseStage.FINAL
        and not observed.is_saas,
        "EXACT_RUNTIME_VERSION_REQUIRED",
    )
    require(
        fingerprint.target_hash == binding["target_hash"],
        "SAVED_TARGET_IDENTITY_DIFFERED",
    )
    require(
        {"base", "product", "stock", "mrp"} <= set(fingerprint.module_versions),
        "MANUFACTURING_MODULES_REQUIRED",
    )
    seed.reader.probe_read_identity((*READ_MODELS, "ir.model.data"))
    models = tuple(sorted(set(SETUP_MODELS + MIGRATION_MODELS)))
    Json2WriteIdentityConnector(seed.write_config).probe_write_identity(
        models, models, creatable_models=models
    )
    require(
        current_principal(seed) == binding["principal_id"],
        "SAVED_MANUAL_PRINCIPAL_DIFFERED",
    )


def check_native_reference(seed, ledger):
    rows = seed.records(
        "ir.model.data",
        ("module", "name", "model", "res_id", "noupdate"),
        (("module", "=", "product"), ("name", "=", "product_category_services")),
        limit=3,
    )
    require(len(rows) == 1, "NATIVE_REFERENCE_MUST_BE_UNIQUE")
    values = rows[0].values
    identifier = relation_id(values["res_id"])
    require(
        identifier
        and values["model"] == "product.category"
        and values["noupdate"] is True,
        "NATIVE_REFERENCE_DIFFERED",
    )
    fields = (
        ("name", "parent_id", "company_id")
        if seed.major == 20
        else ("name", "parent_id")
    )
    categories = seed.records(
        "product.category", fields, (("id", "=", identifier),), limit=3
    )
    require(
        len(categories) == 1
        and categories[0].values["name"] == "Services"
        and not relation_id(categories[0].values["parent_id"]),
        "NATIVE_SERVICES_ROOT_DIFFERED",
    )
    if seed.major == 20:
        require(
            not relation_id(categories[0].values["company_id"]),
            "NATIVE_SERVICES_MUST_BE_SHARED",
        )
    require(
        ledger["references"]
        == {
            "product.category": [
                {
                    "id": identifier,
                    "source_key": "CAT-10",
                    "path": ["Services"],
                    "xmlid": "product.product_category_services",
                    "noupdate": True,
                    "owned": False,
                }
            ]
        },
        "SAVED_NATIVE_REFERENCE_DIFFERED",
    )
    return identifier


def check_owned_m0(seed, ledger, principal):
    """Use exact saved IDs and independently declared setup values; never adopt."""
    counts = {
        "res.company": 1,
        "product.category": 2,
        "product.tag": 2,
        "mrp.workcenter": 4,
        "res.partner": 1,
        "resource.resource": 4,
    }
    origins = {
        "res.partner": "owned_company_generated_partner",
        "resource.resource": "owned_workcentre_generated_resource",
    }
    require(set(ledger["owned"]) == set(counts), "M0_OWNERSHIP_MODELS_DIFFERED")
    owned = {}
    for model, count in counts.items():
        rows = ledger["owned"][model]
        ids = {row["id"] for row in rows}
        require(
            len(rows) == count
            and len(ids) == count
            and all(type(value) is int and value > 0 for value in ids),
            "M0_OWNERSHIP_IDS_DIFFERED",
        )
        require(
            all(
                row
                == {
                    "id": row["id"],
                    "origin": origins.get(model, "explicit_seed_create"),
                }
                for row in rows
            ),
            "M0_OWNERSHIP_ORIGIN_DIFFERED",
        )
        owned[model] = ids
    companies = seed.records(
        "res.company", ("name", "partner_id", "user_ids"), (("id", ">", 0),), limit=4
    )
    by_name = exact_index(
        companies, lambda row: row.values["name"], "COMPANY_NAME_AMBIGUOUS"
    )
    require(set(by_name) == set(NAMES.values()), "EXACT_PRACTICE_COMPANIES_REQUIRED")
    seed.company_ids = {code: by_name[name].odoo_id for code, name in NAMES.items()}
    north, south = seed.company_ids["NORTH"], seed.company_ids["SOUTH"]
    require(
        north == ledger["initial_company"]["id"] and owned["res.company"] == {south},
        "M0_COMPANY_OWNERSHIP_DIFFERED",
    )
    require(
        principal in relation_ids(by_name[NAMES["SOUTH"]].values["user_ids"]),
        "SOUTH_PRINCIPAL_GRANT_MISSING",
    )
    require(
        {relation_id(by_name[NAMES["SOUTH"]].values["partner_id"])}
        == owned["res.partner"],
        "SOUTH_GENERATED_PARTNER_DIFFERED",
    )
    reference = check_native_reference(seed, ledger)
    require(
        reference not in owned["product.category"], "NATIVE_REFERENCE_MUST_NOT_BE_OWNED"
    )
    projection = ManufacturingProjection(
        seed.records, seed.major, seed.company_ids, seed.oracle
    )
    categories = projection._category_index()
    require(
        {
            categories[("Components",)].odoo_id,
            categories[("Components", "Metals")].odoo_id,
        }
        == owned["product.category"],
        "M0_CATEGORY_OWNERSHIP_DIFFERED",
    )
    unit, tags = projection._setup_relations()
    require(set(tags.values()) == owned["product.tag"], "M0_TAG_OWNERSHIP_DIFFERED")
    expected_operations = [
        {
            "kind": "write",
            "model": "res.company",
            "id": north,
            "values": {"name": NAMES["NORTH"]},
        },
        {
            "kind": "create",
            "model": "res.company",
            "id": None,
            "values": {"name": NAMES["SOUTH"], "user_ids": [[4, principal]]},
            "receipt_ids": [south],
        },
    ]
    for path in [("Components",), ("Components", "Metals")]:
        values = {
            "name": path[-1],
            "parent_id": categories[path[:-1]].odoo_id if len(path) > 1 else False,
        }
        if seed.major == 20:
            values["company_id"] = False
        expected_operations.append(
            {
                "kind": "create",
                "model": "product.category",
                "id": None,
                "values": values,
                "receipt_ids": [categories[path].odoo_id],
            }
        )
    for name, identifier in tags.items():
        expected_operations.append(
            {
                "kind": "create",
                "model": "product.tag",
                "id": None,
                "values": {"name": name},
                "receipt_ids": [identifier],
            }
        )
    seen_centres = set()
    seen_resources = set()
    for expected in seed.seed_spec["work_centres"]:
        company_id = seed.company_ids[expected["company_code"]]
        rows = seed.records(
            "mrp.workcenter",
            WORKCENTRE_FIELDS,
            (
                ("id", "in", tuple(owned["mrp.workcenter"])),
                ("code", "=", expected["code"]),
            ),
            company_id=company_id,
            limit=3,
        )
        require(len(rows) == 1, "OWNED_WORKCENTRE_MISSING_OR_AMBIGUOUS")
        row = rows[0]
        require(
            row.values["name"] == expected["name"]
            and relation_id(row.values["company_id"]) == company_id
            and row.values["active"] is True,
            "OWNED_WORKCENTRE_VALUES_DIFFERED",
        )
        resource = relation_id(row.values["resource_id"])
        require(
            resource in owned["resource.resource"], "OWNED_GENERATED_RESOURCE_DIFFERED"
        )
        resources = seed.records(
            "resource.resource",
            ("name", "company_id", "active"),
            (("id", "=", resource),),
            company_id=company_id,
            limit=3,
        )
        require(
            len(resources) == 1
            and resources[0].values["name"] == expected["name"]
            and relation_id(resources[0].values["company_id"]) == company_id
            and resources[0].values["active"] is True,
            "OWNED_RESOURCE_VALUES_DIFFERED",
        )
        seen_centres.add(row.odoo_id)
        seen_resources.add(resource)
        expected_operations.append(
            {
                "kind": "create",
                "model": "mrp.workcenter",
                "id": None,
                "values": {
                    "code": expected["code"],
                    "name": expected["name"],
                    "company_id": company_id,
                },
                "receipt_ids": [row.odoo_id],
            }
        )
    namespace_centres = seed.records(
        "mrp.workcenter", ("code",), (("code", "like", "MA-WC-%"),), limit=10
    )
    require(
        {row.odoo_id for row in namespace_centres} == owned["mrp.workcenter"],
        "M0_WORKCENTRE_NAMESPACE_DIFFERED",
    )
    require(
        seen_centres == owned["mrp.workcenter"]
        and seen_resources == owned["resource.resource"],
        "M0_GENERATED_RESOURCE_SET_DIFFERED",
    )
    operations = ledger["operations"]
    require(
        len(operations) == 10
        and all(row["status"] == "ACKNOWLEDGED" for row in operations),
        "ACKNOWLEDGED_M0_LEDGER_REQUIRED",
    )

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    actual_operations = [
        {
            key: row[key]
            for key in ("kind", "model", "id", "values", "receipt_ids")
            if key in row
        }
        for row in operations
    ]
    require(
        Counter(map(canonical, actual_operations))
        == Counter(map(canonical, expected_operations)),
        "SAVED_M0_OPERATIONS_DIFFERED",
    )
    require(
        not seed.records(
            "product.template",
            ("default_code",),
            (("default_code", "like", "MA-P-%"),),
            limit=70,
        ),
        "FRESH_MORROW_PRODUCT_SCOPE_REQUIRED",
    )
    require(
        not seed.records(
            "product.product",
            ("default_code",),
            (("default_code", "like", "MA-P-%"),),
            limit=70,
        ),
        "FRESH_MORROW_VARIANT_SCOPE_REQUIRED",
    )
    require(
        not seed.records(
            "mrp.bom", ("code",), (("code", "like", "MA-BOM-%"),), limit=20
        ),
        "FRESH_MORROW_BOM_SCOPE_REQUIRED",
    )
    return {
        "categories": len(categories),
        "owned_categories": 2,
        "owned_workcentres": 4,
        "owned_resources": 4,
        "seed_products_before": 0,
        "units_resolved": 1,
    }, unit


def resume_seed(descriptor, root, directory, major):
    """Reconstruct only a bound, acknowledged M0 session after read-only proof."""
    require(
        directory.is_dir()
        and not directory.is_symlink()
        and directory.stat().st_mode & 0o077 == 0,
        "PRIVATE_SEED_DIRECTORY_REQUIRED",
    )
    binding = read_private(directory / BINDING_FILE)
    ledger = read_private(directory / "owned-records.json")
    require(
        binding["contract_version"] == 1
        and binding["purpose"] == "morrow_manual_manufacturing_setup",
        "MANUAL_BINDING_REQUIRED",
    )
    require(
        binding["descriptor_hash"] == digest(descriptor)
        and binding["corpus"] == corpus_binding(root),
        "SAVED_MANUAL_INPUT_BINDING_DIFFERED",
    )
    require(
        binding["m0_ledger_hash"] == digest(directory / "owned-records.json"),
        "SAVED_M0_LEDGER_CHANGED",
    )
    require(
        ledger["state"] == "M0_SEEDED"
        and ledger["major"] == major
        and ledger["database"] == binding["database"]
        and ledger["purpose"] == "morrow_practice_qualification",
        "ACKNOWLEDGED_M0_LEDGER_REQUIRED",
    )
    require(
        not (directory / "post-category-dispatch.json").exists(),
        "POST_CATEGORY_SETUP_ALREADY_DISPATCHED",
    )
    # The constructor only reads the descriptor/keys and creates a new private
    # read-only audit ledger. It does not contact or seed the target.
    audit = directory / ("resume-check-" + uuid4().hex)
    seed = ManufacturingSeedClient(descriptor, root, audit)
    require(seed.major == major == binding["major"], "SAVED_MAJOR_DIFFERED")
    check_runtime(seed, binding)
    counts, unit = check_owned_m0(seed, ledger, binding["principal_id"])
    private_json(
        audit / "verified-m0.json", {"status": "READ_ONLY_VALIDATED", "counts": counts}
    )
    # Continue the same exact private ownership ledger only after every proof.
    seed.private = directory
    seed.ledger_path = directory / "owned-records.json"
    seed.ledger = ledger
    seed.user_id = binding["principal_id"]
    seed.unit_id = unit
    seed.seeded = True
    return seed, counts


def prepare(args, *, corpus_root=ROOT):
    require(args.allow_write is True, "ALLOW_WRITE_REQUIRED")
    require(args.expected_major in (19, 20), "EXACT_MAJOR_REQUIRED")
    root = Path(corpus_root).resolve(strict=True)
    validate_files(root)
    descriptor = Path(args.target_descriptor).expanduser().absolute()
    directory = Path(args.seed_directory).expanduser().absolute()
    if args.stage == "initial":
        seed = ManufacturingSeedClient(descriptor, root, directory)
        require(seed.major == args.expected_major, "DESCRIPTOR_MAJOR_DIFFERED")
        seed.validate()
        counts = seed.seed(allow_write=True)
        fingerprint = seed.reader.get_target_fingerprint()
        binding = {
            "contract_version": 1,
            "purpose": "morrow_manual_manufacturing_setup",
            "descriptor_hash": digest(descriptor),
            "corpus": corpus_binding(root),
            "major": seed.major,
            "database": seed.ledger["database"],
            "principal_id": seed.user_id,
            "target_hash": fingerprint.target_hash,
            "m0_ledger_hash": digest(seed.ledger_path),
        }
        private_json(directory / BINDING_FILE, binding, exclusive=True)
    else:
        require(args.stage == "existing-products", "MANUAL_SETUP_STAGE_REQUIRED")
        seed, checks = resume_seed(descriptor, root, directory, args.expected_major)
        marker = directory / "post-category-dispatch.json"
        private_json(
            marker,
            {"state": "DISPATCHING", "major": seed.major, "read_only_checks": checks},
            exclusive=True,
        )
        try:
            counts = seed.seed_existing_products(allow_write=True)
        except Exception:
            private_json(
                marker,
                {
                    "state": "FAILED_OR_UNKNOWN_RETAIN_FOR_DIAGNOSIS",
                    "major": seed.major,
                },
            )
            raise
        private_json(
            marker, {"state": "ACKNOWLEDGED", "major": seed.major, "counts": counts}
        )
    return {
        "status": "PASSED",
        "stage": args.stage,
        "major": seed.major,
        "counts": counts,
        "migration_loaded": False,
        "services_started": 0,
        "browser_sessions_started": 0,
        "private_evidence_retained": True,
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-descriptor", type=Path, required=True)
    parser.add_argument("--expected-major", type=int, choices=(19, 20), required=True)
    parser.add_argument(
        "--stage", choices=("initial", "existing-products"), required=True
    )
    parser.add_argument("--seed-directory", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        report = prepare(arguments(argv))
    except (
        AssertionError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        KeyboardInterrupt,
    ) as error:
        # Raw exceptions can contain private fixture IDs or paths. Operational
        # evidence stays in the user-selected private ledger and target.
        safe_code = (
            str(error)
            if isinstance(error, RuntimeError)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", str(error))
            else "MANUAL_MANUFACTURING_SETUP_STOPPED"
        )
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "failure_code": safe_code,
                    "target_retained": True,
                }
            )
        )
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
