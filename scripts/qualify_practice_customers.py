"""Qualify the shared customer exercises through normal application services.

Run from the checkout with ``python -m scripts.qualify_practice_customers``.
Controlled runs replace only the Odoo boundary. Live runs require an explicit
disposable descriptor and --allow-write before any credential file is read.
No service or browser is started. No target is automatically cleaned or reset.
The ordinary result contains fixed statuses, counts and hashes only; private
application stores, setup receipts and credential material stay under private/.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import traceback
from datetime import UTC, datetime
from pathlib import Path
from subprocess import SubprocessError

from duckdb import Error as DuckDbError

from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.serialization import content_hash
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts import practice_customer_workflow as workflow_module
from scripts.practice_customer_execution import (
    COUNTS,
    assert_controlled_projection,
    classifications,
    exercise_workspace,
    immediate_overrides,
)
from scripts.practice_customer_workflow import PracticeCustomerWorkflow

EXERCISES = ("enrichment", "payment-terms", "incoming-immediate", "all")
PHASES = {
    "enrichment": ("enrichment",),
    "payment-terms": ("headers", "schedule", "customers"),
    "incoming-immediate": ("incoming-immediate",),
}
POPULATIONS = {
    "enrichment": (18, 9, 9),
    "headers": (4, 2, 2),
    "schedule": (8, 4, 4),
    "customers": (22, 11, 11),
    "incoming-immediate": (22, 4, 18),
}
JOBS = (
    "load_jobs",
    "preparation_jobs",
    "odoo_capture_jobs",
    "recipe_run_jobs",
    "preflight_jobs",
)


class PracticeQualificationError(RuntimeError):
    """Carry only a reviewed fixed reason code into the portable result."""

    def __init__(self, code):
        super().__init__(code)
        self.safe_code = code


def require(condition, code):
    if not condition:
        raise PracticeQualificationError(code)


def _write_private(path, value):
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    with Path(path).open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(payload)


def _corpus_root():
    return (
        Path(workflow_module.__file__).resolve().parents[1]
        / "scenarios/impodo-practice/v1"
    )


def _source_hash(root):
    files = sorted(root.rglob("*.csv"))
    require(bool(files), "PRACTICE_INPUTS_MISSING")
    return content_hash(
        {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        }
    )


def _refs(company):
    prefix = "MA-N" if company == "NORTH" else "MA-S"
    return (prefix + "-0001", prefix + "-0003")


def _recipe_row_confirmation(fixture, application, phase, *, expected_population=None):
    """Confirm the fresh checked population through the normal guarded services."""
    if application.status.value == "READY":
        return application
    context, actor = fixture.context, fixture.actor
    issues = context.run_planning.repository.list_issues(application.application_id)
    blocking = {item.code for item in issues if item.blocks}
    require(
        blocking == {"MAPPING_ROW_INCLUSION_CONFIRMATION_REQUIRED"},
        "RECIPE_APPLICABILITY_BLOCKED",
    )
    mappings = context.mapping_workspace.mappings
    revision = mappings.get_mapping_revision(application.workspace_id)
    working = mappings.get_mapping_working_draft(application.workspace_id)
    require(revision is not None and working is not None, "RECIPE_MAPPING_UNAVAILABLE")
    validation = mappings.get_mapping_validation(
        application.workspace_id, revision.version
    )
    require(
        validation is not None and validation.status.value != "INVALID",
        "RECIPE_MAPPING_INVALID",
    )
    review = run_local_operation_sync(
        context.row_inclusion_reviews.check_current,
        application.workspace_id,
        actor=actor,
    )
    require(
        (review.source_row_count, review.included_count, review.excluded_count)
        == (
            expected_population
            if expected_population is not None
            else POPULATIONS[phase]
        ),
        "RECIPE_POPULATION_DIFFERED",
    )
    require(review.cannot_evaluate_count == 0, "RECIPE_POPULATION_UNCHECKED")
    run_local_operation_sync(
        context.row_inclusion_reviews.confirm_current,
        application.workspace_id,
        datasets=revision.definition.datasets,
        expected_revision_version=revision.version,
        expected_working_draft_version=working.version,
        actor=actor,
    )
    run_local_operation_sync(
        context.mapping_workspace.submit_current,
        application.workspace_id,
        datasets=revision.definition.datasets,
        expected_version=revision.version,
        expected_working_draft_version=working.version,
        actor=actor,
        warning_acknowledgements=tuple(
            mapping_issue_fingerprint(item)
            for item in validation.issues
            if item.severity == "warning"
        ),
    )
    confirmed = run_local_operation_sync(
        context.run_planning.confirm_application_mapping,
        application.application_id,
        actor=actor,
    )
    require(confirmed.status.value == "READY", "RECIPE_APPLICABILITY_BLOCKED")
    return confirmed


def _controlled_other_company(target, company):
    other = "SOUTH" if company == "NORTH" else "NORTH"
    prefix = "MA-S-" if other == "SOUTH" else "MA-N-"
    terms = {
        identifier
        for (model, identifier), row in target.records.items()
        if model == "account.payment.term"
        and row.get("company_id") == target.company_ids[other]
    }
    return copy.deepcopy(
        {
            key: row
            for key, row in target.records.items()
            if (key[0] == "res.partner" and str(row.get("ref", "")).startswith(prefix))
            or (key[0] == "account.payment.term" and key[1] in terms)
            or (
                key[0] == "account.payment.term.line" and row.get("payment_id") in terms
            )
            or (key[0] == "res.company" and key[1] == target.company_ids[other])
        }
    )


def _controlled_fresh_projection(fixture, target, root, *, company, phase, refs):
    """Read May's independent oracle; do not compare fresh data to April answers."""
    if phase not in {"enrichment", "customers", "incoming-immediate"}:
        assert_controlled_projection(
            fixture, target, root, company=company, phase=phase, refs=refs
        )
        return
    oracle = json.loads((root / "expected-business-results.json").read_text())
    expected = [
        row
        for row in oracle["fresh_customer_delivery"]["customers"]
        if row["company_code"] == company and (not refs or row["customer_ref"] in refs)
    ]
    for row in expected:
        actual = [
            values
            for (model, _), values in target.records.items()
            if model == "res.partner" and values.get("ref") == row["customer_ref"]
        ]
        require(len(actual) == 1, "INDEPENDENT_PROJECTION_DIFFERED")
        actual = actual[0]
        require(
            all(
                (actual.get(field) or "") == (row[field] or "")
                for field in ("name", "email", "phone")
            ),
            "INDEPENDENT_PROJECTION_DIFFERED",
        )
        require(
            all(
                actual.get(field) == row["address"][source]
                for field, source in (
                    ("street", "street"),
                    ("zip", "postcode"),
                    ("city", "city"),
                )
            ),
            "INDEPENDENT_PROJECTION_DIFFERED",
        )
        if phase != "enrichment":
            term = target.records[
                ("account.payment.term", actual["property_payment_term_id"])
            ]
            name = (
                "Morrow Immediate"
                if phase == "incoming-immediate"
                else "Morrow Net " + row["payment_term_key"][1][3:]
            )
            require(
                term["name"] == name
                and term["company_id"] == target.company_ids[company],
                "INDEPENDENT_PROJECTION_DIFFERED",
            )
    if phase == "incoming-immediate":
        for (model, identifier), term in target.records.items():
            if (
                model != "account.payment.term"
                or term["company_id"] != target.company_ids[company]
            ):
                continue
            lines = [
                row
                for (kind, _), row in target.records.items()
                if kind == "account.payment.term.line"
                and row["payment_id"] == identifier
            ]
            require(len(lines) == 1, "INDEPENDENT_SCHEDULE_DIFFERED")
            days = 0 if term["name"] == "Morrow Immediate" else 30
            require(
                (
                    lines[0]["value"],
                    lines[0]["value_amount"],
                    lines[0]["nb_days"],
                    lines[0]["delay_type"],
                )
                == ("percent", 100, days, "days_after"),
                "INDEPENDENT_SCHEDULE_DIFFERED",
            )


def _phase_result(value):
    """Allowlist count-and-hash fields from the operational workflow helper."""
    required = {
        "phase",
        "first_comparison",
        "committed",
        "verified",
        "repeat_unchanged",
        "snapshot_hash",
        "reconciliation_hash",
        "persisted_reopen_verified",
    }
    require(required <= value.keys(), "WORKFLOW_RECEIPT_INCOMPLETE")
    require(value["phase"] in COUNTS, "WORKFLOW_RECEIPT_INVALID")
    counts = value["first_comparison"]
    require(
        set(counts) == {"CREATE", "UPDATE", "UNCHANGED", "BLOCKED", "AMBIGUOUS"}
        and all(type(number) is int and number >= 0 for number in counts.values()),
        "WORKFLOW_RECEIPT_INVALID",
    )
    require(
        all(
            type(value[key]) is int and value[key] >= 0
            for key in ("committed", "verified", "repeat_unchanged")
        ),
        "WORKFLOW_RECEIPT_INVALID",
    )
    require(
        all(
            isinstance(value[key], str)
            and value[key].startswith("sha256:")
            and len(value[key]) == 71
            for key in ("snapshot_hash", "reconciliation_hash")
        ),
        "WORKFLOW_RECEIPT_INVALID",
    )
    require(value["persisted_reopen_verified"] is True, "WORKFLOW_REOPEN_UNVERIFIED")
    return {key: value[key] for key in sorted(required)}


def _retain_and_close(fixture, private, *, controlled):
    """Always close after a vault or copy failure; never claim a failed copy."""
    if controlled:
        try:
            fixture.client.close()
            for name in JOBS:
                manager = getattr(fixture.context, name, None)
                if manager is not None:
                    manager.shutdown()
            _write_private(private / "vault.json", dict(fixture.secrets.values))
            shutil.copytree(Path(fixture.temporary.name), private / "app")
        finally:
            fixture.tearDown()
    else:
        try:
            _write_private(private / "vault.json", dict(fixture.secrets.values))
        finally:
            fixture.close()


def _close_and_record_retention(fixture, private, *, controlled, report):
    """Publish success only after retention and lifecycle closure both finish."""
    report["private_evidence_retained"] = False
    try:
        _retain_and_close(fixture, private, controlled=controlled)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        report["private_evidence_failure_code"] = "PRIVATE_EVIDENCE_RETENTION_FAILED"
        raise PracticeQualificationError("PRIVATE_EVIDENCE_RETENTION_FAILED") from error
    report["private_evidence_retained"] = True


def _exercise(args, root, private, exercise, *, report):
    controlled = args.controlled
    fixture = target = seed = None
    result = {
        "exercise": exercise,
        "phases": [],
        "status": "FAILED",
        "excluded_company_unchanged": False,
        "inputs": [],
    }
    report["exercises"].append(result)
    other = "SOUTH" if args.company == "NORTH" else "NORTH"
    try:
        if controlled:
            from tests.support.practice_customer_target import PracticeCustomerTarget

            fixture = PracticeCustomerWorkflow(methodName="runTest")
            fixture.setUp()
            target = PracticeCustomerTarget(
                root / "target-seed-projection.json",
                major=args.expected_major,
                mode=args.mode,
                company=args.company,
            )
            before = _controlled_other_company(target, args.company)
        else:
            # The authorization check in qualify() must precede these imports,
            # constructors, descriptor reads, and all credential reads.
            from scripts.practice_customer_live import (
                LivePracticeCustomerWorkflow,
                PracticeConnection,
            )
            from scripts.practice_customer_seed import PracticeSeedClient

            report["phase"] = "target_descriptor"
            connection = PracticeConnection.read(
                args.target_descriptor, expected_major=args.expected_major
            )
            require(connection.mode == args.mode, "DESCRIPTOR_MODE_DIFFERED")
            seed = PracticeSeedClient(args.target_descriptor, root, private / "seed")
            report["phase"] = "target_validation"
            seed.validate()
            report["phase"] = "target_seed"
            seed.seed(allow_write=True)
            seed.verify_seed()
            before = seed.snapshot_excluded_company(company=other)
            fixture = LivePracticeCustomerWorkflow(connection, private / "app")

        for phase in PHASES[exercise]:
            report["phase"] = phase + "_authoring"
            refs = _refs(args.company) if phase == "incoming-immediate" else ()
            baseline_overrides = (
                immediate_overrides(root, args.company) if refs else None
            )
            delivery = "2026-05-15" if args.entrypoint == "recipe" else "2026-04-15"
            effective_overrides = (
                immediate_overrides(root, args.company, delivery=delivery)
                if refs
                else None
            )
            effective = fixture.practice_files(
                root,
                phase=phase,
                delivery=delivery,
                source_overrides=effective_overrides,
            )
            result["inputs"].append(
                {
                    "phase": phase,
                    "delivery": delivery,
                    "variant": "incoming-immediate-v1" if refs else "baseline",
                    "file_hashes": {
                        name + ".csv": "sha256:" + hashlib.sha256(data).hexdigest()
                        for name, data in sorted(effective.items())
                    },
                }
            )
            workspace_id, schema, _revision = fixture.author(
                root=root,
                target=target,
                major=args.expected_major,
                mode=args.mode,
                company=args.company,
                phase=phase,
                source_overrides=baseline_overrides,
                customer_refs=refs,
            )
            version = recognize_odoo_version(schema.odoo_version)
            require(
                (version.major, version.release_stage)
                == (args.expected_major, OdooReleaseStage.FINAL),
                "CAPTURED_ODOO_VERSION_DIFFERED",
            )
            report["verified_major"] = version.major
            report["verified_series"] = version.series
            report["verified_release_stage"] = version.release_stage.value
            if args.entrypoint == "recipe":
                # Author and publish without writing. The fresh application is
                # the first writer, so its reviewed original counts still apply.
                fixture.prepare(workspace_id)
                fixture.approve(workspace_id)
                publication = fixture.publish_practice_recipe(
                    workspace_id, label="Morrow " + phase
                )
                fresh_overrides = (
                    immediate_overrides(root, args.company, delivery="2026-05-15")
                    if refs
                    else None
                )
                report["phase"] = phase + "_recipe_application"
                application, _schema = fixture.apply_practice_recipe(
                    publication,
                    source_workspace_id=workspace_id,
                    root=root,
                    target=target,
                    major=args.expected_major,
                    mode=args.mode,
                    company=args.company,
                    phase=phase,
                    source_overrides=fresh_overrides,
                )
                application = _recipe_row_confirmation(fixture, application, phase)
                workspace_id = application.workspace_id
            report["phase"] = phase + "_execution"
            receipt = exercise_workspace(
                fixture,
                workspace_id,
                phase=phase,
                target=target,
                expected=classifications(*COUNTS[phase]),
            )
            report["phase"] = phase + "_projection"
            fresh = args.entrypoint == "recipe"
            if controlled:
                if fresh:
                    _controlled_fresh_projection(
                        fixture,
                        target,
                        root,
                        company=args.company,
                        phase=phase,
                        refs=refs,
                    )
                else:
                    assert_controlled_projection(
                        fixture,
                        target,
                        root,
                        company=args.company,
                        phase=phase,
                        refs=refs,
                    )
            elif phase in {"enrichment", "customers", "incoming-immediate"}:
                seed.verify_customers(
                    company=args.company,
                    fresh=fresh,
                    immediate_refs=refs,
                    with_terms=phase != "enrichment",
                )
                if phase == "incoming-immediate":
                    seed.verify_terms(
                        company=args.company,
                        expected={"Morrow Net 30": 30, "Morrow Immediate": 0},
                    )
            elif phase == "headers":
                seed.verify_terms(
                    company=args.company,
                    expected={"Morrow Net 30": 30, "Morrow Net 45": 0},
                )
            elif phase == "schedule":
                seed.verify_terms(company=args.company)
            result["phases"].append(_phase_result(receipt))
        report["phase"] = "excluded_company_projection"
        if controlled:
            require(
                _controlled_other_company(target, args.company) == before,
                "EXCLUDED_COMPANY_CHANGED",
            )
        else:
            seed.verify_excluded_company(before, company=other)
        result["excluded_company_unchanged"] = True
        result["status"] = "PASSED"
    finally:
        if fixture is not None:
            _close_and_record_retention(
                fixture, private, controlled=controlled, report=report
            )


def qualify(args, *, corpus_root=None):
    """Retain a fresh sanitized receipt, including every failed prerequisite."""
    output = Path(args.output_directory).expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    report = {
        "contract_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "FAILED",
        "failure_code": "",
        "phase": "authorization",
        "evidence_kind": "CONTROLLED_APPLICATION" if args.controlled else "LIVE_ODOO",
        "expected_major": args.expected_major,
        "mode": args.mode,
        "company": args.company,
        "entrypoint": args.entrypoint,
        "exercise": args.exercise,
        "services_started": 0,
        "browser_sessions_started": 0,
        "private_evidence_retained": True,
        "automatic_recovery_implemented": False,
        "target_cleanup": "NOT_APPLICABLE"
        if args.controlled
        else "NOT_IMPLEMENTED_RETAINED_DISPOSABLE_TARGET",
        "exercises": [],
    }
    try:
        require(args.controlled or args.allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(
            args.controlled or args.exercise != "all",
            "LIVE_ALL_REQUIRES_SEPARATE_TARGETS",
        )
        require(
            args.expected_major in (19, 20)
            and args.mode in ("LOCAL", "REMOTE")
            and args.company in ("NORTH", "SOUTH")
            and args.entrypoint in ("authoring", "recipe")
            and args.exercise in EXERCISES,
            "QUALIFICATION_OPTIONS_INVALID",
        )
        root = Path(corpus_root) if corpus_root is not None else _corpus_root()
        report["source_set_hash"] = _source_hash(root)
        report["business_expectations_hash"] = (
            "sha256:"
            + hashlib.sha256(
                (root / "expected-business-results.json").read_bytes()
            ).hexdigest()
        )
        report["target_seed_hash"] = (
            "sha256:"
            + hashlib.sha256(
                (root / "target-seed-projection.json").read_bytes()
            ).hexdigest()
        )
        selected = tuple(PHASES) if args.exercise == "all" else (args.exercise,)
        for exercise in selected:
            exercise_private = private / exercise
            exercise_private.mkdir(mode=0o700)
            _exercise(args, root, exercise_private, exercise, report=report)
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
            else "QUALIFICATION_PHASE_FAILED"
        )
        _write_private(
            private / "failure.json",
            {"type": type(error).__name__, "traceback": traceback.format_exc()},
        )
    finally:
        payload = json.dumps(report, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        with (output / "result.json").open("xb") as stream:
            os.chmod(output / "result.json", 0o600)
            stream.write(payload)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--controlled", action="store_true")
    target.add_argument("--target-descriptor", type=Path)
    parser.add_argument("--expected-major", type=int, choices=(19, 20), required=True)
    parser.add_argument("--mode", choices=("LOCAL", "REMOTE"), required=True)
    parser.add_argument("--company", choices=("NORTH", "SOUTH"), required=True)
    parser.add_argument("--exercise", choices=EXERCISES, required=True)
    parser.add_argument("--entrypoint", choices=("authoring", "recipe"), required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        report = qualify(arguments(argv))
    except (OSError, ValueError):
        print("PRACTICE_OUTPUT_DIRECTORY_UNAVAILABLE")
        return 2
    print("Practice qualification " + report["status"].lower() + ".")
    return 0 if report["status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
