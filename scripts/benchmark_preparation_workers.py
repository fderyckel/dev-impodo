"""Measure first/repeat preparation in fresh production worker processes.

Each outer run creates one deterministic project, executes first preparation,
deletes the registered source artifact, and executes repeat preparation from
the immutable prepared snapshot. The harness records worker CPU, peak working
set, wall time, database/project storage, stable hashes, and worker exit.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "IMPODO_PREPARATION_WORKER_BENCHMARK_JSON="
TEST_NAME = (
    "tests.performance.test_preparation_scale.PreparationWorkflowScaleTests."
    "test_background_worker_releases_its_working_memory"
)


class PreparationWorkerBenchmarkError(RuntimeError):
    """Raised when worker qualification evidence cannot be trusted."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--columns", type=int, default=30)
    parser.add_argument("--mapped-fields", type=int, default=20)
    parser.add_argument("--effect-fields", type=int, default=1)
    parser.add_argument("--products", type=int, default=16_000)
    parser.add_argument("--bom-lines", type=int, default=80_000)
    parser.add_argument(
        "--workload",
        choices=("products", "bom", "customers", "product-bom"),
        default="products",
    )
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="Permit diagnostic evidence from an uncommitted revision.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path)
    return parser


def run_fresh_processes(arguments: argparse.Namespace) -> dict[str, object]:
    _validate_arguments(arguments)
    revision = _revision()
    worktree_fingerprint = _worktree_fingerprint(
        expected_revision=revision,
    )
    worktree_dirty = _worktree_dirty()
    _require_worktree_unchanged(worktree_fingerprint)
    if worktree_dirty and not arguments.allow_dirty_worktree:
        raise PreparationWorkerBenchmarkError(
            "Qualification evidence requires a clean worktree; commit the "
            "intended revision or use --allow-dirty-worktree for a dry run"
        )
    results = []
    for run_number in range(1, arguments.runs + 1):
        _require_worktree_unchanged(worktree_fingerprint)
        try:
            result = _run_once(
                arguments,
                revision=revision,
                run_number=run_number,
            )
        except Exception as error:
            _require_worktree_unchanged(
                worktree_fingerprint,
                cause=error,
            )
            raise
        _require_worktree_unchanged(worktree_fingerprint)
        results.append(result)
    comparable_results = tuple(results)
    _require_comparable_results(comparable_results)
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "command": {
            "bom_lines": (
                arguments.bom_lines if arguments.workload == "product-bom" else None
            ),
            "columns": arguments.columns,
            "dirty": arguments.dirty,
            "effect_fields": arguments.effect_fields,
            "mapped_fields": arguments.mapped_fields,
            "products": (
                arguments.products if arguments.workload == "product-bom" else None
            ),
            "rows": arguments.rows,
            "runs": arguments.runs,
            "workload": arguments.workload,
        },
        "result_schema_version": 1,
        "revision": revision,
        "runs": list(comparable_results),
        "summary": summarize(comparable_results),
        "worktree_fingerprint": worktree_fingerprint,
        "worktree_dirty": worktree_dirty,
    }
    vectorization = comparable_results[0].get("vectorization_report")
    if isinstance(vectorization, dict):
        report["vectorization_report"] = vectorization
    return report


def summarize(results: Iterable[dict[str, object]]) -> dict[str, object]:
    items = tuple(results)
    if not items:
        raise PreparationWorkerBenchmarkError("At least one result is required")
    summary: dict[str, object] = {"run_count": len(items)}
    for attempt in ("first", "repeat"):
        attempt_items = tuple(_attempt(item, attempt) for item in items)
        for metric in ("cpu_seconds", "peak_worker_mib", "wall_seconds"):
            values = tuple(float(item[metric]) for item in attempt_items)
            summary[f"median_{attempt}_{metric}"] = statistics.median(values)
            summary[f"maximum_{attempt}_{metric}"] = max(values)
        for metric in (
            "database_file_bytes",
            "database_used_bytes",
            "project_storage_bytes",
        ):
            values = tuple(
                float(_storage(item)[metric]) / (1024 * 1024) for item in attempt_items
            )
            summary[f"median_{attempt}_{metric.removesuffix('_bytes')}_mib"] = (
                statistics.median(values)
            )
    parent_deltas = tuple(
        float(_parent_rss(item)["repeat_delta_mib"]) for item in items
    )
    summary["maximum_parent_repeat_delta_mib"] = max(parent_deltas)
    summary["median_parent_repeat_delta_mib"] = statistics.median(parent_deltas)
    return summary


def extract_result(output: str) -> dict[str, object]:
    marked = [
        line.removeprefix(RESULT_PREFIX)
        for line in output.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(marked) != 1:
        raise PreparationWorkerBenchmarkError(
            "Worker child did not emit exactly one structured result"
        )
    try:
        payload = json.loads(marked[0])
    except json.JSONDecodeError as error:
        raise PreparationWorkerBenchmarkError(
            "Worker child emitted invalid JSON"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PreparationWorkerBenchmarkError(
            "Worker child emitted an unsupported result schema"
        )
    return payload


def _run_once(
    arguments: argparse.Namespace,
    *,
    revision: str,
    run_number: int,
) -> dict[str, object]:
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    environment.update(
        {
            "IMPODO_PREPARATION_BENCHMARK_REVISION": revision,
            "IMPODO_PREPARATION_SCALE_COLUMNS": str(arguments.columns),
            "IMPODO_PREPARATION_SCALE_DIRTY": "1" if arguments.dirty else "0",
            "IMPODO_PREPARATION_SCALE_EFFECT_FIELDS": str(arguments.effect_fields),
            "IMPODO_PREPARATION_SCALE_MAPPED_FIELDS": str(arguments.mapped_fields),
            "IMPODO_PREPARATION_SCALE_PRODUCTS": str(arguments.products),
            "IMPODO_PREPARATION_SCALE_BOM_LINES": str(arguments.bom_lines),
            "IMPODO_PREPARATION_SCALE_ROWS": str(arguments.rows),
            "IMPODO_PREPARATION_SCALE_WORKLOAD": arguments.workload,
            "IMPODO_PREPARATION_WORKER_JSON": "1",
            "IMPODO_RUN_PREPARATION_SCALE": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", TEST_NAME, "-v"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=arguments.timeout_seconds,
        check=False,
    )
    combined_output = completed.stdout + "\n" + completed.stderr
    if completed.returncode != 0:
        tail = "\n".join(combined_output.splitlines()[-40:])
        raise PreparationWorkerBenchmarkError(
            f"Worker benchmark child {run_number} failed:\n{tail}"
        )
    result = extract_result(combined_output)
    result["fresh_outer_process_run"] = run_number
    return result


def _require_comparable_results(
    results: tuple[dict[str, object], ...],
) -> None:
    first = results[0]
    invariant_keys = (
        "columns",
        "dirty",
        "effect_fields",
        "hashes",
        "mapped_fields",
        "platform",
        "revision",
        "rows",
        "runtime_versions",
        "vectorization_report",
        "workload",
    )
    first_fixture = _fixture(first)
    for result in results:
        if any(result.get(key) != first.get(key) for key in invariant_keys):
            raise PreparationWorkerBenchmarkError(
                "Fresh worker results are not comparable"
            )
        fixture = _fixture(result)
        if fixture != first_fixture:
            raise PreparationWorkerBenchmarkError(
                "Fresh worker fixtures are not byte-identical"
            )
        if not result.get("workers_exited"):
            raise PreparationWorkerBenchmarkError("A worker did not exit")
        if result.get("source_reopened"):
            raise PreparationWorkerBenchmarkError(
                "Repeat preparation reopened the registered source"
            )
        if not result.get("prepared_snapshot_reused"):
            raise PreparationWorkerBenchmarkError(
                "Repeat preparation did not reuse its prepared snapshot"
            )


def _attempt(result: dict[str, object], name: str) -> dict[str, object]:
    value = result.get(name)
    if not isinstance(value, dict):
        raise PreparationWorkerBenchmarkError(f"Missing {name} attempt evidence")
    return value


def _storage(attempt: dict[str, object]) -> dict[str, object]:
    value = attempt.get("storage")
    if not isinstance(value, dict):
        raise PreparationWorkerBenchmarkError("Missing storage evidence")
    return value


def _fixture(result: dict[str, object]) -> dict[str, object]:
    value = result.get("fixture")
    if not isinstance(value, dict):
        raise PreparationWorkerBenchmarkError("Missing fixture evidence")
    return value


def _parent_rss(result: dict[str, object]) -> dict[str, object]:
    value = result.get("parent_rss")
    if not isinstance(value, dict):
        raise PreparationWorkerBenchmarkError("Missing parent RSS evidence")
    return value


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.runs < 1 or arguments.rows < 1:
        raise PreparationWorkerBenchmarkError("Runs and rows must be positive")
    if arguments.columns < 3:
        raise PreparationWorkerBenchmarkError(
            "Worker benchmark requires at least three columns"
        )
    if not 3 <= arguments.mapped_fields <= arguments.columns:
        raise PreparationWorkerBenchmarkError(
            "Mapped fields must be between three and the column count"
        )
    if not 1 <= arguments.effect_fields < arguments.mapped_fields:
        raise PreparationWorkerBenchmarkError(
            "Effect fields must be positive and exclude the identity field"
        )
    if arguments.workload == "product-bom":
        if min(arguments.products, arguments.bom_lines) < 1:
            raise PreparationWorkerBenchmarkError(
                "Related Product/BOM counts must be positive"
            )
        if arguments.columns < 4 or arguments.mapped_fields < 4:
            raise PreparationWorkerBenchmarkError(
                "Related Product/BOM requires at least four columns"
            )
        if arguments.dirty:
            raise PreparationWorkerBenchmarkError(
                "The related Product/BOM fixture has its own deterministic shape"
            )
    if arguments.timeout_seconds < 1:
        raise PreparationWorkerBenchmarkError("Timeout must be positive")
    if arguments.output is not None and not (
        arguments.output.expanduser().resolve().parent.is_dir()
    ):
        raise PreparationWorkerBenchmarkError(
            "Worker benchmark output parent directory does not exist"
        )


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise PreparationWorkerBenchmarkError("Cannot identify Git revision")
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
        raise PreparationWorkerBenchmarkError("Cannot inspect Git worktree")
    return bool(completed.stdout.strip())


def _worktree_fingerprint(*, expected_revision: str | None = None) -> str:
    """Identify the exact commit plus tracked and untracked state for one run."""

    revision = _revision()
    if expected_revision is not None and revision != expected_revision:
        raise PreparationWorkerBenchmarkError(
            "Git HEAD changed before the worker benchmark state was captured"
        )
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
        raise PreparationWorkerBenchmarkError("Cannot fingerprint the Git worktree")
    if _revision() != revision:
        raise PreparationWorkerBenchmarkError(
            "Git HEAD changed while the worker benchmark state was captured"
        )
    digest = sha256()
    digest.update(b"HEAD\0")
    digest.update(revision.encode("utf-8"))
    digest.update(b"\0")
    digest.update(status.stdout)
    digest.update(b"\0")
    digest.update(diff.stdout)
    try:
        for relative_bytes in sorted(filter(None, untracked.stdout.split(b"\0"))):
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
        raise PreparationWorkerBenchmarkError(
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
    error = PreparationWorkerBenchmarkError(
        "The Git worktree changed while the worker benchmark was running; "
        "discard this mixed-build evidence and rerun from a stable revision"
    )
    if cause is not None:
        raise error from cause
    raise error


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = run_fresh_processes(arguments)
    except (PreparationWorkerBenchmarkError, subprocess.TimeoutExpired) as error:
        print(f"Worker preparation benchmark failed: {error}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.expanduser().resolve().write_text(
            encoded,
            encoding="utf-8",
            newline="\n",
        )
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
