"""Qualify an accepted hierarchy against an explicitly allocated disposable Odoo.

Run with ``python -m scripts.qualify_hierarchy_workflow``. The descriptor holds
routing and an existing credential-owner UUID, never API keys. The runner
reads exact role envelopes through CredentialVault and creates only the fixed
fictional hierarchy in a checked-empty fixture scope. It starts no services.
Success includes authoring, Recipe replay, reconciliation, repeat comparison,
and exact fixture cleanup. Failures retain private evidence and vault keys.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from impodo.adapters.protected_evidence.credential_vault import CredentialVault
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.shared.models import canonical_json_bytes
from scripts.hierarchy_qualification_journey import FaultObserver, LiveHierarchyJourney
from scripts.hierarchy_qualification_target import (
    FixtureTarget,
    OwnedVault,
    QualificationError,
    TargetDescriptor,
    require,
)

FAULTS = (
    "none",
    "before-dispatch",
    "after-commit",
    "after-receipt",
    "known-rejection",
    "unknown-outcome",
)


def qualify(
    args,
    *,
    vault=None,
    journey_factory=LiveHierarchyJourney,
    target_factory=FixtureTarget,
):
    """Always retain a sanitized terminal result; never clean an uncertain target."""

    output = Path(args.output_directory).expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    private = output / "private"
    private.mkdir(mode=0o700)
    report = {
        "contract_version": 1,
        "case_id": str(uuid4()),
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "FAILED",
        "phase": "authorization",
        "failure_code": "",
        "source_shape": args.source_shape
        if args.source_shape in {"generated", "supplied"}
        else "INVALID",
        "connection_mode": args.mode if args.mode in {"LOCAL", "REMOTE"} else "INVALID",
        "expected_major": args.expected_major
        if args.expected_major in {19, 20}
        else None,
        "injected_fault": args.fault if args.fault in FAULTS else "INVALID",
        "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "cleanup": "NOT_NEEDED",
        "private_evidence_retained": True,
        "services_started": 0,
        "phases": [],
    }
    journey = owned_vault = target = observer = None
    try:
        require(args.allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(
            args.source_shape in {"generated", "supplied"} and args.fault in FAULTS,
            "SCENARIO_INVALID",
        )
        report["phase"] = "descriptor"
        descriptor = TargetDescriptor.read(
            args.target_descriptor, mode=args.mode, major=args.expected_major
        )
        report["source_commit"] = descriptor.source_commit
        report["lab_target"] = descriptor.lab_target
        report["phase"] = "credentials"
        vault = vault if vault is not None else CredentialVault()
        keys = descriptor.credentials(vault)
        owned_vault = OwnedVault(vault, private / "vault-entries.json")
        report["phase"] = "target_validation"
        phase_private = private / "authoring"
        phase_private.mkdir(mode=0o700)
        target = target_factory(descriptor, *keys, phase_private)
        target.validate()
        report["observed_version"] = f"{descriptor.major}.0"
        report["phase"] = "seed"
        target.seed()
        report["cleanup"] = "RETAINED_FOR_DIAGNOSIS"
        report["phase"] = "authoring"
        journey = journey_factory(descriptor, keys, owned_vault, private / "workspace")
        workspace_id, _ = journey.hierarchy(
            major=descriptor.major, mode=descriptor.mode, source_shape=args.source_shape
        )
        preview = journey.prepare_review(workspace_id)
        project_id, publication = journey.publish_recipe(workspace_id)
        observer = FaultObserver(args.fault)
        report["phase"] = "authoring_load"
        run = journey.load(workspace_id, preview, observer)
        target.adopt_verified(run)
        target.verify_business()
        report["fault_triggered"] = observer.fired
        require(args.fault == "none" or observer.fired, "REQUESTED_FAULT_NOT_REACHED")
        report["phases"].append(
            {
                "name": "authoring",
                "committed": run.committed_count,
                "verified": 18,
                "generated_variants": target.variant_count,
            }
        )
        report["phase"] = "repeat_comparison"
        repeat = journey.compare(workspace_id)
        require(
            (repeat.create_count, repeat.update_count, repeat.unchanged_count)
            == (0, 0, 18),
            "REPEAT_COMPARISON_DIFFERED",
        )
        report["phase"] = "authoring_cleanup"
        target.cleanup()
        target = None
        report["cleanup"] = "COMPLETE"
        report["phase"] = "recipe_target_validation"
        phase_private = private / "recipe"
        phase_private.mkdir(mode=0o700)
        target = target_factory(descriptor, *keys, phase_private)
        target.validate()
        report["phase"] = "recipe_seed"
        target.seed()
        report["cleanup"] = "RETAINED_FOR_DIAGNOSIS"
        report["phase"] = "recipe_application"
        workspace_id = journey.replay_recipe(project_id, publication, args.source_shape)
        preview = journey.prepare_review(workspace_id)
        report["phase"] = "recipe_load"
        run = journey.load(workspace_id, preview, FaultObserver("none"))
        target.adopt_verified(run)
        target.verify_business()
        report["phases"].append(
            {
                "name": "recipe",
                "committed": run.committed_count,
                "verified": 18,
                "generated_variants": target.variant_count,
            }
        )
        report["phase"] = "recipe_cleanup"
        target.cleanup()
        target = None
        report["cleanup"] = "COMPLETE"
        report["database_timings"] = journey.metrics
        report["phase"] = "credential_cleanup"
        owned_vault.cleanup()
        report["status"], report["phase"] = "PASSED", "complete"
    except BaseException as error:  # noqa: BLE001 -- Persist a sanitized terminal receipt even on interruption.
        report["failure_code"] = (
            error.safe_code
            if isinstance(error, QualificationError)
            else "QUALIFICATION_PHASE_FAILED"
        )
        if target is not None and target.seeded:
            report["cleanup"] = (
                "INCOMPLETE"
                if "cleanup" in report["phase"]
                else "RETAINED_FOR_DIAGNOSIS"
            )
    finally:
        report["fault_triggered"] = bool(observer and observer.fired)
        report["observed_accepted_batches"] = (
            observer.accepted_batches if observer else 0
        )
        report["observed_accepted_rows"] = observer.accepted_rows if observer else 0
        if journey is not None:
            try:
                journey.close()
            except BaseException:  # noqa: BLE001 -- Close errors must not erase the terminal receipt.
                report["status"], report["failure_code"] = (
                    "FAILED",
                    "LOCAL_RUNTIME_CLOSE_FAILED",
                )
        # This allowlist is constructed here; exception text, descriptor routing,
        # source values, target IDs, credentials and private manifests never enter it.
        (output / "result.json").write_bytes(canonical_json_bytes(report))
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-descriptor", type=Path, required=True)
    parser.add_argument(
        "--source-shape", choices=("generated", "supplied"), required=True
    )
    parser.add_argument("--mode", choices=("LOCAL", "REMOTE"), required=True)
    parser.add_argument("--expected-major", type=int, choices=(19, 20), required=True)
    parser.add_argument("--fault", choices=FAULTS, default="none")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        result = qualify(arguments(argv))
    except (OSError, ValueError):
        print("Qualification could not create its fresh result directory.")
        return 2
    print("Hierarchy qualification " + result["status"].lower() + ".")
    return 0 if result["status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
