"""Run reproducible fresh-process local preparation benchmarks.

The child process exercises the existing opt-in preparation scale test and
emits one machine-readable result.  This parent starts a new interpreter for
every run so allocator state and retained project objects cannot leak between
measurements.  It records no source row values.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "IMPODO_PREPARATION_BENCHMARK_JSON="
TEST_NAME = (
    "tests.test_preparation_scale.PreparationWorkflowScaleTests."
    "test_complete_preparation_workflow"
)


class PreparationBenchmarkError(RuntimeError):
    """Raised when a fresh benchmark process fails or emits invalid evidence."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--columns", type=int, default=30)
    parser.add_argument("--mapped-fields", type=int, default=20)
    parser.add_argument("--workload", choices=("products", "bom"), default="products")
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument("--advanced", action="store_true")
    parser.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="Permit a diagnostic run that is not acceptable baseline evidence.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON evidence path; parent directories must already exist.",
    )
    return parser


def run_fresh_processes(arguments: argparse.Namespace) -> dict[str, object]:
    _validate_arguments(arguments)
    revision = _revision()
    worktree_dirty = _worktree_dirty()
    if worktree_dirty and not arguments.allow_dirty_worktree:
        raise PreparationBenchmarkError(
            "Baseline evidence requires a clean worktree; commit the intended "
            "implementation or use --allow-dirty-worktree for diagnostics"
        )
    results = tuple(
        _run_once(arguments, revision=revision, run_number=index)
        for index in range(1, arguments.runs + 1)
    )
    _require_comparable_results(results)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "command": {
            "advanced": arguments.advanced,
            "columns": arguments.columns,
            "dirty": arguments.dirty,
            "mapped_fields": arguments.mapped_fields,
            "rows": arguments.rows,
            "runs": arguments.runs,
            "workload": arguments.workload,
        },
        "result_schema_version": 1,
        "revision": revision,
        "runs": list(results),
        "summary": summarize(results),
        "worktree_dirty": worktree_dirty,
    }


def summarize(results: Iterable[dict[str, object]]) -> dict[str, object]:
    """Return medians without hiding the individual fresh-process evidence."""

    items = tuple(results)
    if not items:
        raise PreparationBenchmarkError("At least one benchmark result is required")
    return {
        "median_cpu_seconds": statistics.median(
            float(item["cpu_seconds"]) for item in items
        ),
        "median_database_mib": statistics.median(
            float(item["database_mib"]) for item in items
        ),
        "median_ending_rss_mib": statistics.median(
            float(item["ending_rss_mib"]) for item in items
        ),
        "median_peak_working_set_mib": statistics.median(
            float(item["peak_working_set_mib"]) for item in items
        ),
        "median_wall_seconds": statistics.median(
            float(item["wall_seconds"]) for item in items
        ),
        "run_count": len(items),
    }


def extract_result(output: str) -> dict[str, object]:
    """Extract exactly one marked JSON object from unittest output."""

    marked = [
        line.removeprefix(RESULT_PREFIX)
        for line in output.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(marked) != 1:
        raise PreparationBenchmarkError(
            "Benchmark child did not emit exactly one structured result"
        )
    try:
        payload = json.loads(marked[0])
    except json.JSONDecodeError as error:
        raise PreparationBenchmarkError(
            "Benchmark child emitted invalid JSON"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PreparationBenchmarkError(
            "Benchmark child emitted an unsupported result schema"
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
    current_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not current_python_path
        else source_path + os.pathsep + current_python_path
    )
    environment.update(
        {
            "IMPODO_PREPARATION_ADVANCED": "1" if arguments.advanced else "0",
            "IMPODO_PREPARATION_BENCHMARK_REVISION": revision,
            "IMPODO_PREPARATION_SCALE_COLUMNS": str(arguments.columns),
            "IMPODO_PREPARATION_SCALE_DIRTY": "1" if arguments.dirty else "0",
            "IMPODO_PREPARATION_SCALE_JSON": "1",
            "IMPODO_PREPARATION_SCALE_MAPPED_FIELDS": str(
                arguments.mapped_fields
            ),
            "IMPODO_PREPARATION_SCALE_ROWS": str(arguments.rows),
            "IMPODO_PREPARATION_SCALE_WORKLOAD": arguments.workload,
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
        raise PreparationBenchmarkError(
            f"Benchmark child {run_number} failed:\n{tail}"
        )
    result = extract_result(combined_output)
    result["fresh_process_run"] = run_number
    return result


def _require_comparable_results(results: tuple[dict[str, object], ...]) -> None:
    first = results[0]
    invariant_keys = (
        "columns",
        "dirty",
        "mapped_fields",
        "revision",
        "rows",
        "runtime_versions",
        "workload",
    )
    for result in results[1:]:
        if any(result.get(key) != first.get(key) for key in invariant_keys):
            raise PreparationBenchmarkError(
                "Fresh-process benchmark results are not comparable"
            )
        first_fixture = first.get("fixture")
        fixture = result.get("fixture")
        if not isinstance(first_fixture, dict) or not isinstance(fixture, dict):
            raise PreparationBenchmarkError("Benchmark fixture evidence is missing")
        if (
            fixture.get("sha256") != first_fixture.get("sha256")
            or fixture.get("size_bytes") != first_fixture.get("size_bytes")
        ):
            raise PreparationBenchmarkError(
                "Fresh-process benchmark fixtures are not byte-identical"
            )


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if arguments.runs < 1:
        raise PreparationBenchmarkError("Benchmark run count must be positive")
    if arguments.rows < 1:
        raise PreparationBenchmarkError("Benchmark row count must be positive")
    if arguments.columns < 3:
        raise PreparationBenchmarkError("Benchmark requires at least three columns")
    if not 3 <= arguments.mapped_fields <= arguments.columns:
        raise PreparationBenchmarkError(
            "Mapped fields must be between three and the source column count"
        )
    if arguments.timeout_seconds < 1:
        raise PreparationBenchmarkError("Benchmark timeout must be positive")
    if arguments.output is not None:
        output = arguments.output.expanduser().resolve()
        if not output.parent.is_dir():
            raise PreparationBenchmarkError(
                "Benchmark output parent directory does not exist"
            )


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PreparationBenchmarkError("Cannot identify the benchmark revision")
    return completed.stdout.strip()


def _worktree_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PreparationBenchmarkError("Cannot inspect the benchmark worktree")
    return bool(completed.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = run_fresh_processes(arguments)
    except (PreparationBenchmarkError, subprocess.TimeoutExpired) as error:
        print(f"Preparation benchmark failed: {error}", file=sys.stderr)
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
