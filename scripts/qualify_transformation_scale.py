"""Run the Phase 7 transformation-scale qualification matrix.

The release profile executes three fresh outer processes for every fixture.
Each outer process performs first and repeat preparation in separate production
workers, proves worker exit and prepared-snapshot reuse, and records CPU, wall
time, peak working set, database use, project storage, and semantic hashes.

Use the smoke profile to validate the harness quickly on any development host.
Only a clean release-profile run on Windows can report release-qualified.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


class TransformationQualificationError(RuntimeError):
    """Raised when qualification cannot produce trustworthy evidence."""


@dataclass(frozen=True, slots=True)
class WorkerScenario:
    name: str
    workload: str
    rows: int
    columns: int
    mapped_fields: int
    effect_fields: int
    dirty: bool = False
    products: int | None = None
    bom_lines: int | None = None
    peak_limit_mib: float = 900.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("smoke", "release"),
        default="smoke",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".tmp" / "transformation-scale-qualification.json",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / ".tmp" / "transformation-scale-qualification",
    )
    parser.add_argument("--timeout-per-scenario", type=int, default=3_600)
    parser.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="Allow a diagnostic run; it can never be release-qualified.",
    )
    parser.add_argument(
        "--require-release-qualified",
        action="store_true",
        help="Return a failure unless every gate passes on clean Windows.",
    )
    return parser


def scenarios(profile: str) -> tuple[WorkerScenario, ...]:
    if profile == "release":
        return (
            WorkerScenario(
                name="direct_products_100k",
                workload="products",
                rows=100_000,
                columns=30,
                mapped_fields=20,
                effect_fields=1,
                peak_limit_mib=750.0,
            ),
            WorkerScenario(
                name="wide_customer_twin_1k",
                workload="customers",
                rows=1_000,
                columns=150,
                mapped_fields=20,
                effect_fields=1,
                peak_limit_mib=500.0,
            ),
            WorkerScenario(
                name="related_product_bom_96k",
                workload="product-bom",
                rows=96_000,
                columns=10,
                mapped_fields=6,
                effect_fields=1,
                products=16_000,
                bom_lines=80_000,
            ),
            WorkerScenario(
                name="effect_heavy_4k",
                workload="products",
                rows=4_000,
                columns=20,
                mapped_fields=20,
                effect_fields=19,
            ),
            WorkerScenario(
                name="dirty_high_effect_4k",
                workload="products",
                rows=4_000,
                columns=20,
                mapped_fields=20,
                effect_fields=19,
                dirty=True,
            ),
        )
    if profile != "smoke":
        raise TransformationQualificationError("Unknown qualification profile")
    return (
        WorkerScenario(
            name="direct_products_smoke",
            workload="products",
            rows=500,
            columns=10,
            mapped_fields=6,
            effect_fields=1,
            peak_limit_mib=750.0,
        ),
        WorkerScenario(
            name="wide_customer_smoke",
            workload="customers",
            rows=100,
            columns=30,
            mapped_fields=10,
            effect_fields=1,
            peak_limit_mib=500.0,
        ),
        WorkerScenario(
            name="related_product_bom_smoke",
            workload="product-bom",
            rows=600,
            columns=6,
            mapped_fields=4,
            effect_fields=1,
            products=100,
            bom_lines=500,
        ),
        WorkerScenario(
            name="effect_heavy_smoke",
            workload="products",
            rows=200,
            columns=10,
            mapped_fields=10,
            effect_fields=9,
        ),
        WorkerScenario(
            name="dirty_high_effect_smoke",
            workload="products",
            rows=200,
            columns=10,
            mapped_fields=10,
            effect_fields=9,
            dirty=True,
        ),
    )


def run_qualification(arguments: argparse.Namespace) -> dict[str, object]:
    _validate_arguments(arguments)
    revision = _revision()
    worktree_fingerprint = _worktree_fingerprint()
    worktree_dirty = _worktree_dirty()
    _require_worktree_unchanged(worktree_fingerprint)
    if worktree_dirty and not arguments.allow_dirty_worktree:
        raise TransformationQualificationError(
            "Qualification requires a clean worktree; commit the combined "
            "revision or use --allow-dirty-worktree for a smoke/dry run"
        )
    evidence_dir = arguments.evidence_dir.expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    run_count = 3 if arguments.profile == "release" else 1
    scenario_reports: dict[str, object] = {}
    gate_results: list[dict[str, object]] = []
    commands: dict[str, list[str]] = {}
    for scenario in scenarios(arguments.profile):
        _require_worktree_unchanged(worktree_fingerprint)
        evidence_path = evidence_dir / f"{scenario.name}.json"
        command = _worker_command(
            scenario,
            runs=run_count,
            evidence_path=evidence_path,
            timeout_seconds=min(arguments.timeout_per_scenario, 900),
            allow_dirty=worktree_dirty,
        )
        commands[scenario.name] = command
        try:
            _run_command(
                command,
                name=scenario.name,
                timeout_seconds=arguments.timeout_per_scenario,
            )
        except Exception as error:
            _require_worktree_unchanged(
                worktree_fingerprint,
                cause=error,
            )
            raise
        _require_worktree_unchanged(worktree_fingerprint)
        report = _load_json(evidence_path)
        _require_revision(report, revision, scenario.name)
        scenario_reports[scenario.name] = report
        gate_results.extend(
            _worker_gates(
                scenario,
                report,
                expected_runs=run_count,
            )
        )

    relationship_path = evidence_dir / "relationship_semantic_parity.json"
    relationship_command = _relationship_command(
        profile=arguments.profile,
        runs=run_count,
        evidence_path=relationship_path,
    )
    commands["relationship_semantic_parity"] = relationship_command
    _require_worktree_unchanged(worktree_fingerprint)
    try:
        _run_command(
            relationship_command,
            name="relationship_semantic_parity",
            timeout_seconds=arguments.timeout_per_scenario,
        )
    except Exception as error:
        _require_worktree_unchanged(
            worktree_fingerprint,
            cause=error,
        )
        raise
    _require_worktree_unchanged(worktree_fingerprint)
    relationship_report = _load_json(relationship_path)
    _require_revision(
        relationship_report,
        revision,
        "relationship_semantic_parity",
    )
    scenario_reports["relationship_semantic_parity"] = relationship_report
    gate_results.extend(
        _relationship_gates(
            relationship_report,
            expected_runs=run_count,
        )
    )

    is_windows = platform.system() == "Windows"
    context_gates = (
        _gate("release_profile", arguments.profile, "eq", "release"),
        _gate("three_fresh_runs", run_count, "eq", 3),
        _gate("clean_worktree", worktree_dirty, "eq", False),
        _gate("reference_windows_host", is_windows, "eq", True),
    )
    performance_gates_passed = all(
        bool(item["passed"]) for item in gate_results
    )
    context_gates_passed = all(
        bool(item["passed"]) for item in context_gates
    )
    release_qualified = performance_gates_passed and context_gates_passed
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "commands": commands,
        "context_gates": list(context_gates),
        "gate_results": gate_results,
        "performance_gates_passed": performance_gates_passed,
        "platform": platform.platform(),
        "profile": arguments.profile,
        "release_qualified": release_qualified,
        "result_schema_version": 1,
        "revision": revision,
        "run_count": run_count,
        "scenarios": scenario_reports,
        "worktree_fingerprint": worktree_fingerprint,
        "worktree_dirty": worktree_dirty,
    }


def _worker_command(
    scenario: WorkerScenario,
    *,
    runs: int,
    evidence_path: Path,
    timeout_seconds: int,
    allow_dirty: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "benchmark_preparation_workers.py"),
        "--runs",
        str(runs),
        "--workload",
        scenario.workload,
        "--rows",
        str(scenario.rows),
        "--columns",
        str(scenario.columns),
        "--mapped-fields",
        str(scenario.mapped_fields),
        "--effect-fields",
        str(scenario.effect_fields),
        "--timeout-seconds",
        str(timeout_seconds),
        "--output",
        str(evidence_path),
    ]
    if scenario.dirty:
        command.append("--dirty")
    if scenario.products is not None:
        command.extend(("--products", str(scenario.products)))
    if scenario.bom_lines is not None:
        command.extend(("--bom-lines", str(scenario.bom_lines)))
    if allow_dirty:
        command.append("--allow-dirty-worktree")
    return command


def _relationship_command(
    *,
    profile: str,
    runs: int,
    evidence_path: Path,
) -> list[str]:
    products, bom_lines = (
        (16_000, 80_000) if profile == "release" else (100, 500)
    )
    return [
        sys.executable,
        str(ROOT / "scripts" / "benchmark_relationships.py"),
        "--runs",
        str(runs),
        "--products",
        str(products),
        "--bom-lines",
        str(bom_lines),
        "--batch-size",
        "5000",
        "--output",
        str(evidence_path),
    ]


def _worker_gates(
    scenario: WorkerScenario,
    report: dict[str, object],
    *,
    expected_runs: int,
) -> list[dict[str, object]]:
    summary = _dict(report, "summary")
    prefix = scenario.name
    return [
        _gate(
            f"{prefix}.fresh_run_count",
            _number(summary, "run_count"),
            "eq",
            expected_runs,
        ),
        _gate(
            f"{prefix}.first_cpu_observed",
            _number(summary, "maximum_first_cpu_seconds"),
            "gt",
            0.0,
        ),
        _gate(
            f"{prefix}.repeat_cpu_observed",
            _number(summary, "maximum_repeat_cpu_seconds"),
            "gt",
            0.0,
        ),
        _gate(
            f"{prefix}.first_peak_observed",
            _number(summary, "maximum_first_peak_worker_mib"),
            "gt",
            0.0,
        ),
        _gate(
            f"{prefix}.repeat_peak_observed",
            _number(summary, "maximum_repeat_peak_worker_mib"),
            "gt",
            0.0,
        ),
        _gate(
            f"{prefix}.first_wall_seconds",
            _number(summary, "maximum_first_wall_seconds"),
            "lt",
            120.0,
        ),
        _gate(
            f"{prefix}.repeat_wall_seconds",
            _number(summary, "maximum_repeat_wall_seconds"),
            "lt",
            120.0,
        ),
        _gate(
            f"{prefix}.first_peak_worker_mib",
            _number(summary, "maximum_first_peak_worker_mib"),
            "lt",
            scenario.peak_limit_mib,
        ),
        _gate(
            f"{prefix}.repeat_peak_worker_mib",
            _number(summary, "maximum_repeat_peak_worker_mib"),
            "lt",
            scenario.peak_limit_mib,
        ),
        _gate(
            f"{prefix}.parent_repeat_delta_mib",
            _number(summary, "maximum_parent_repeat_delta_mib"),
            "lt",
            64.0,
        ),
    ]


def _relationship_gates(
    report: dict[str, object],
    *,
    expected_runs: int,
) -> list[dict[str, object]]:
    runs = report.get("runs")
    if not isinstance(runs, list) or not runs:
        raise TransformationQualificationError(
            "Relationship parity evidence has no runs"
        )
    hybrids = []
    for pair in runs:
        if not isinstance(pair, dict):
            raise TransformationQualificationError(
                "Relationship parity run is invalid"
            )
        hybrid = pair.get("set_based_hybrid")
        if not isinstance(hybrid, dict):
            raise TransformationQualificationError(
                "Relationship set-based evidence is missing"
            )
        hybrids.append(hybrid)
    return [
        _gate(
            "relationship_semantic_parity.fresh_run_count",
            len(runs),
            "eq",
            expected_runs,
        ),
        _gate(
            "relationship_semantic_parity.peak_observed",
            max(_number(item, "peak_rss_mib") for item in hybrids),
            "gt",
            0.0,
        ),
        _gate(
            "relationship_semantic_parity.wall_seconds",
            max(_number(item, "wall_seconds") for item in hybrids),
            "lt",
            120.0,
        ),
        _gate(
            "relationship_semantic_parity.peak_rss_mib",
            max(_number(item, "peak_rss_mib") for item in hybrids),
            "lt",
            900.0,
        ),
    ]


def _gate(
    name: str,
    actual: object,
    operator: str,
    threshold: object,
) -> dict[str, object]:
    if operator == "lt":
        passed = float(actual) < float(threshold)
    elif operator == "gt":
        passed = float(actual) > float(threshold)
    elif operator == "eq":
        passed = actual == threshold
    else:
        raise TransformationQualificationError(f"Unsupported gate: {operator}")
    return {
        "actual": actual,
        "name": name,
        "operator": operator,
        "passed": passed,
        "threshold": threshold,
    }


def _run_command(
    command: Sequence[str],
    *,
    name: str,
    timeout_seconds: int,
) -> None:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode:
        output = completed.stdout + "\n" + completed.stderr
        tail = "\n".join(output.splitlines()[-50:])
        raise TransformationQualificationError(
            f"Qualification scenario {name} failed:\n{tail}"
        )


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TransformationQualificationError(
            f"Qualification evidence is invalid: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise TransformationQualificationError(
            f"Qualification evidence is not an object: {path}"
        )
    return payload


def _require_revision(
    report: dict[str, object],
    revision: str,
    name: str,
) -> None:
    if report.get("revision") != revision:
        raise TransformationQualificationError(
            f"Qualification scenario {name} used another revision"
        )


def _dict(value: dict[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise TransformationQualificationError(f"Missing object: {key}")
    return item


def _number(value: dict[str, object], key: str) -> float:
    item = value.get(key)
    if not isinstance(item, (int, float)):
        raise TransformationQualificationError(f"Missing number: {key}")
    return float(item)


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.timeout_per_scenario < 1:
        raise TransformationQualificationError(
            "Scenario timeout must be positive"
        )
    output = arguments.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise TransformationQualificationError("Cannot identify Git revision")
    return completed.stdout.strip()


def _worktree_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise TransformationQualificationError("Cannot inspect Git worktree")
    return bool(completed.stdout.strip())


def _worktree_fingerprint() -> str:
    """Identify the exact tracked and untracked worktree state for one run."""

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if status.returncode or diff.returncode or untracked.returncode:
        raise TransformationQualificationError(
            "Cannot fingerprint the Git worktree"
        )
    digest = sha256()
    digest.update(status.stdout)
    digest.update(b"\0")
    digest.update(diff.stdout)
    try:
        for relative_bytes in sorted(
            filter(None, untracked.stdout.split(b"\0"))
        ):
            path = ROOT / os.fsdecode(relative_bytes)
            digest.update(b"\0untracked\0")
            digest.update(relative_bytes)
            if path.is_symlink():
                digest.update(os.fsencode(os.readlink(path)))
                continue
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    except OSError as error:
        raise TransformationQualificationError(
            "Cannot fingerprint the Git worktree"
        ) from error
    return digest.hexdigest()


def _require_worktree_unchanged(
    expected: str,
    *,
    cause: Exception | None = None,
) -> None:
    if _worktree_fingerprint() == expected:
        return
    error = TransformationQualificationError(
        "The Git worktree changed while qualification was running; discard "
        "this mixed-build evidence and rerun from a stable revision"
    )
    if cause is not None:
        raise error from cause
    raise error


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = run_qualification(arguments)
    except (TransformationQualificationError, subprocess.TimeoutExpired) as error:
        print(f"Transformation qualification failed: {error}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    arguments.output.expanduser().resolve().write_text(
        encoded,
        encoding="utf-8",
        newline="\n",
    )
    print(encoded, end="")
    if not report["performance_gates_passed"]:
        return 1
    if arguments.require_release_qualified and not report["release_qualified"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
