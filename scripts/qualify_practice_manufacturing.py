"""Run shared product, BOM, component-line and operation practice workflows.

Controlled runs replace only Odoo. Live runs require an explicitly allocated
fresh disposable target and --allow-write before descriptor/credential access.
The seven stages use normal source decisions, authoring or fresh Recipe runs,
comparison, persisted reopening, journalled loading and independent read-back.
Targets and private evidence are retained; no service/browser or cleanup runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import traceback
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from subprocess import SubprocessError

from duckdb import Error as DuckDbError

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.shared.models import TargetRecord
from scripts.practice_customer_execution import classifications, exercise_workspace
from scripts.practice_manufacturing_live import (
    ControlledManufacturingWorkflow,
    LiveManufacturingWorkflow,
)
from scripts.practice_manufacturing_recipe_review import confirm_manufacturing_recipe
from scripts.practice_manufacturing_workflow import MANUFACTURING_COUNTS
from scripts.qualify_practice_customers import (
    PracticeQualificationError,
    _close_and_record_retention,
    _source_hash,
    _write_private,
    require,
)

ROOT = (
    Path(__file__).resolve().parents[1] / "scenarios/impodo-practice/manufacturing/v1"
)
STAGES = (
    ("categories", "NORTH"),
    ("products", "NORTH"),
    ("products", "SOUTH"),
    ("product-status", "NORTH"),
    ("product-status", "SOUTH"),
    ("boms", "NORTH"),
    ("boms", "SOUTH"),
)
POPULATIONS = {
    "categories": (12, 12, 0),
    "products": (32, 16, 16),
    "product-status": (32, 16, 16),
    "boms": (42, 21, 21),
}


def validate_files(root):
    """Require the reviewed bytes before starting a target interaction."""
    manifest = json.loads((root / "manifest.json").read_text())
    for item in manifest["files"]:
        path = root / item["path"]
        require(
            not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),
            "MANUFACTURING_INPUT_OUTSIDE_PACK",
        )
        raw = path.read_bytes()
        require(
            len(raw) == item["bytes"]
            and hashlib.sha256(raw).hexdigest() == item["sha256"],
            "MANUFACTURING_INPUT_HASH_DIFFERED",
        )


def check_snapshot(fixture, snapshot, *, phase, company):
    """Check every model before dispatch, including unchanged dependencies."""
    rows = Counter((row.target_model, row.disposition) for row in snapshot.rows)
    if phase == "categories":
        expected = {"product.category": (9, 0, 3)}
    elif phase in {"products", "product-status"}:
        expected = {
            "product.category": (0, 0, 12),
            "product.template": (0, 1, 15)
            if phase == "product-status"
            else (
                12,
                0 if company == "NORTH" else 1,
                4 if company == "NORTH" else 3,
            ),
        }
    else:
        expected = {
            "mrp.bom": (3, 0, 0),
            "mrp.bom.line": (12, 0, 0),
            "mrp.routing.workcenter": (6, 0, 0),
        }
    fixture.assertEqual({row.target_model for row in snapshot.rows}, set(expected))
    for model, counts in expected.items():
        fixture.assertEqual(
            {kind: rows[model, kind] for kind in classifications(0, 0, 0)},
            classifications(*counts),
        )
    if phase == "product-status":
        changed = [row for row in snapshot.rows if row.disposition == "UPDATE"]
        fixture.assertEqual(len(changed), 1)
        fixture.assertEqual(
            {field.field for field in changed[0].fields if field.action != "SKIP"},
            {"active"},
        )
    if phase == "boms":
        parents = {
            row.row_id: row for row in snapshot.rows if row.target_model == "mrp.bom"
        }
        for row in snapshot.rows:
            if row.target_model not in {"mrp.bom.line", "mrp.routing.workcenter"}:
                continue
            dependency = {
                key for field in row.fields for key in field.dependency_row_ids
            } & parents.keys()
            fixture.assertEqual(len(dependency), 1)
            fixture.assertLess(
                parents[next(iter(dependency))].schedule_ordinal, row.schedule_ordinal
            )


def controlled_records(target):
    def records(model, fields, domain, *, company_id=None, limit=100):
        del company_id
        rows = tuple(
            TargetRecord(
                model,
                identifier,
                {field: target.value(model, identifier, field) for field in fields},
            )
            for (kind, identifier) in sorted(target.records)
            if kind == model and target.matches(model, identifier, domain)
        )
        require(len(rows) < limit, "CONTROLLED_PROJECTION_LIMIT_REACHED")
        return rows

    return records


def qualify(args, *, corpus_root=ROOT):
    output = Path(args.output_directory).expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    report = {
        "contract_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "component": "impodo-practice/manufacturing/v1",
        "status": "FAILED",
        "phase": "authorization",
        "failure_code": "",
        "expected_major": args.expected_major,
        "mode": args.mode,
        "entrypoint": args.entrypoint,
        "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "evidence_kind": "CONTROLLED_APPLICATION" if args.controlled else "LIVE_ODOO",
        "services_started": 0,
        "browser_sessions_started": 0,
        "private_evidence_retained": True,
        "automatic_recovery_implemented": False,
        "target_cleanup": "NOT_APPLICABLE"
        if args.controlled
        else "RETAINED_DISPOSABLE_TARGET",
        "phases": [],
        "inputs": [],
    }
    fixture = target = seed = None
    try:
        require(args.controlled or args.allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(
            args.expected_major in (19, 20)
            and args.mode in ("LOCAL", "REMOTE")
            and args.entrypoint in ("authoring", "recipe"),
            "MANUFACTURING_OPTIONS_INVALID",
        )
        root = Path(corpus_root)
        validate_files(root)
        report["source_set_hash"] = _source_hash(root)
        for name, key in (
            ("expected-business-results.json", "business_expectations_hash"),
            ("target-seed-projection.json", "target_seed_hash"),
        ):
            report[key] = (
                "sha256:" + hashlib.sha256((root / name).read_bytes()).hexdigest()
            )
        from scripts.practice_manufacturing_seed import (
            ManufacturingProjection,
            ManufacturingSeedClient,
        )

        oracle = json.loads((root / "expected-business-results.json").read_text())[
            "baseline"
        ]
        if args.controlled:
            from tests.support.practice_manufacturing_target import (
                PracticeManufacturingTarget,
            )

            fixture = ControlledManufacturingWorkflow(methodName="runTest")
            fixture.setUp()
            target = PracticeManufacturingTarget(
                root / "target-seed-projection.json",
                major=args.expected_major,
                mode=args.mode,
            )
            projection = ManufacturingProjection(
                controlled_records(target),
                args.expected_major,
                target.company_ids,
                oracle,
            )
        else:
            from scripts.practice_customer_live import PracticeConnection

            report["phase"] = "target_descriptor"
            connection = PracticeConnection.read(
                args.target_descriptor, expected_major=args.expected_major
            )
            require(connection.mode == args.mode, "DESCRIPTOR_MODE_DIFFERED")
            seed = ManufacturingSeedClient(
                args.target_descriptor, root, private / "seed"
            )
            report["phase"] = "initial_target_seed"
            seed.validate()
            seed.seed(allow_write=True)
            fixture = LiveManufacturingWorkflow(connection, private / "app")
            projection = ManufacturingProjection(
                seed.records, args.expected_major, seed.company_ids, oracle
            )
        for phase, company in STAGES:
            report["phase"] = phase + "_" + company
            other_company = "SOUTH" if company == "NORTH" else "NORTH"
            excluded_before = (
                projection.snapshot_company(other_company) if phase == "boms" else None
            )
            workspace, schema, _revision = fixture.author(
                root=root,
                target=target,
                major=args.expected_major,
                mode=args.mode,
                phase=phase,
                company=company,
            )
            observed = recognize_odoo_version(schema.odoo_version)
            require(
                (observed.major, observed.release_stage)
                == (args.expected_major, OdooReleaseStage.FINAL),
                "CAPTURED_ODOO_VERSION_DIFFERED",
            )
            report["verified_series"] = observed.series
            if args.entrypoint == "recipe":
                fixture.prepare(workspace)
                fixture.approve(workspace)
                publication = fixture.publish_practice_recipe(
                    workspace,
                    label="Morrow manufacturing " + phase,
                    business_purpose="Reuse reviewed Morrow manufacturing exercise rules",
                )
                application, _schema = fixture.apply_practice_recipe(
                    publication,
                    source_workspace_id=workspace,
                    root=root,
                    target=target,
                    major=args.expected_major,
                    mode=args.mode,
                    phase=phase,
                    company=company,
                    run_label="Fresh Morrow manufacturing " + phase + " " + company,
                )
                application = confirm_manufacturing_recipe(
                    fixture, application, phase, expected_population=POPULATIONS[phase]
                )
                workspace = application.workspace_id
            delivery = "2026-05-15" if args.entrypoint == "recipe" else "2026-04-15"
            files = fixture.practice_files(root, phase=phase, delivery=delivery)
            report["inputs"].append(
                {
                    "phase": phase,
                    "company": company,
                    "delivery": delivery,
                    "file_hashes": {
                        name + ".csv": "sha256:" + hashlib.sha256(raw).hexdigest()
                        for name, raw in sorted(files.items())
                    },
                }
            )
            receipt = exercise_workspace(
                fixture,
                workspace,
                phase=phase,
                target=target,
                expected=classifications(*MANUFACTURING_COUNTS[phase, company]),
                inspect_snapshot=partial(
                    check_snapshot, fixture, phase=phase, company=company
                ),
            )
            receipt["company"] = company
            if phase == "categories":
                projection.verify_categories()
                if target is not None:
                    target.seed_existing_products()
                else:
                    seed.seed_existing_products(allow_write=True)
            elif phase == "products":
                projection.verify_products(company, initial=True)
                if company == "SOUTH":
                    projection.verify_products(initial=True)
            elif phase == "product-status":
                projection.verify_products(company)
                if company == "SOUTH":
                    projection.verify_products()
            else:
                projection.verify_boms(company)
                require(
                    projection.snapshot_company(other_company) == excluded_before,
                    "EXCLUDED_COMPANY_GRAPH_CHANGED",
                )
                receipt["excluded_company_unchanged"] = True
            report["phases"].append(receipt)
        report["status"], report["phase"] = "PASSED", "complete"
    except (
        AssertionError,
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        SubprocessError,
        DuckDbError,
        KeyboardInterrupt,
    ) as error:
        report["failure_code"] = (
            error.safe_code
            if isinstance(error, PracticeQualificationError)
            else "MANUFACTURING_PHASE_FAILED"
        )
        _write_private(
            private / "failure.json",
            {"type": type(error).__name__, "traceback": traceback.format_exc()},
        )
    finally:
        if fixture is not None:
            try:
                _close_and_record_retention(
                    fixture, private, controlled=args.controlled, report=report
                )
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                report["status"], report["failure_code"] = (
                    "FAILED",
                    "PRIVATE_EVIDENCE_RETENTION_FAILED",
                )
                _write_private(
                    private / "retention-failure.json",
                    {"type": type(error).__name__, "traceback": traceback.format_exc()},
                )
        _write_private(output / "result.json", report)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--controlled", action="store_true")
    target.add_argument("--target-descriptor", type=Path)
    parser.add_argument("--expected-major", type=int, choices=(19, 20), required=True)
    parser.add_argument("--mode", choices=("LOCAL", "REMOTE"), required=True)
    parser.add_argument("--entrypoint", choices=("authoring", "recipe"), required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        report = qualify(arguments(argv))
    except (OSError, ValueError):
        print("MANUFACTURING_OUTPUT_DIRECTORY_UNAVAILABLE")
        return 2
    print("Manufacturing qualification " + report["status"].lower() + ".")
    return 0 if report["status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
