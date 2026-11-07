"""Prepare only the disposable Morrow baseline for a manual Impodo exercise.

Run with ``python -m scripts.prepare_practice_target`` from the checkout.
This command validates and seeds the named fresh target. It opens no browser,
starts no service, and runs no import workflow. Connect your existing Impodo
session to that target afterward and use the canonical practice CSVs.
The target and private seed ledger are retained; no cleanup or retry runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path

from impodo.domain.serialization import content_hash
from scripts import practice_customer_workflow as workflow_module


class PracticeTargetPreparationError(RuntimeError):
    """Carry a fixed public code without descriptor or credential values."""

    def __init__(self, code):
        super().__init__(code)
        self.safe_code = code


def require(condition, code):
    if not condition:
        raise PracticeTargetPreparationError(code)


def _write_json(path, value):
    with Path(path).open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(
            json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        )


def prepare(args, *, connection_reader=None, seed_factory=None, corpus_root=None):
    """Check explicit write authorization before any descriptor/key read."""
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
        "evidence_kind": "LIVE_ODOO_SEED_ONLY",
        "expected_major": args.expected_major,
        "mode": args.mode,
        "services_started": 0,
        "browser_sessions_started": 0,
        "workflow_runs_started": 0,
        "target_cleanup": "NOT_IMPLEMENTED_TARGET_RETAINED",
        "private_evidence_retained": True,
        "automatic_retry_implemented": False,
    }
    try:
        require(args.allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(
            args.expected_major in (19, 20) and args.mode in ("LOCAL", "REMOTE"),
            "PREPARATION_OPTIONS_INVALID",
        )
        # These imports and constructors are deliberately after authorization.
        if connection_reader is None:
            from scripts.practice_customer_live import PracticeConnection

            connection_reader = PracticeConnection.read
        report["phase"] = "target_descriptor"
        connection = connection_reader(
            args.target_descriptor, expected_major=args.expected_major
        )
        require(connection.mode == args.mode, "DESCRIPTOR_MODE_DIFFERED")
        root = (
            Path(corpus_root)
            if corpus_root is not None
            else (
                Path(workflow_module.__file__).resolve().parents[1]
                / "scenarios/impodo-practice/v1"
            )
        )
        report["seed_specification_hash"] = content_hash(
            {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "target-seed-projection.json",
                    "expected-business-results.json",
                )
            }
        )
        if seed_factory is None:
            from scripts.practice_customer_seed import PracticeSeedClient

            seed_factory = PracticeSeedClient
        client = seed_factory(args.target_descriptor, root, private / "seed")
        report["phase"] = "target_validation"
        client.validate()
        report["phase"] = "target_seed"
        counts = client.seed(allow_write=True)
        report["phase"] = "seed_verification"
        client.verify_seed()
        keys = ("seed_contacts", "seed_terms", "seed_term_lines", "companies")
        require(
            all(type(counts.get(key)) is int and counts[key] >= 0 for key in keys),
            "SEED_RECEIPT_INVALID",
        )
        require(
            counts.get("major") == args.expected_major
            and counts.get("net45_seeded") is False,
            "SEED_RECEIPT_INVALID",
        )
        report["seed_counts"] = {key: counts[key] for key in keys}
        report["net45_seeded"] = False
        report["status"], report["phase"] = "PASSED", "complete"
    except (
        AssertionError,
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        KeyboardInterrupt,
    ) as error:
        report["failure_code"] = (
            error.safe_code
            if isinstance(error, PracticeTargetPreparationError)
            else "TARGET_PREPARATION_PHASE_FAILED"
        )
        _write_json(
            private / "failure.json",
            {"type": type(error).__name__, "traceback": traceback.format_exc()},
        )
    finally:
        _write_json(output / "result.json", report)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-descriptor", type=Path, required=True)
    parser.add_argument("--expected-major", type=int, choices=(19, 20), required=True)
    parser.add_argument("--mode", choices=("LOCAL", "REMOTE"), required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        report = prepare(arguments(argv))
    except (OSError, ValueError):
        print("PRACTICE_OUTPUT_DIRECTORY_UNAVAILABLE")
        return 2
    print("Practice target preparation " + report["status"].lower() + ".")
    return 0 if report["status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
