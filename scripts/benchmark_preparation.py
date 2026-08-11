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
SUPPORTED_CHILD_SCHEMAS = frozenset({1, 2})
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
    parser.add_argument(
        "--workload",
        choices=("products", "bom", "customers"),
        default="products",
    )
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument("--advanced", action="store_true")
    parser.add_argument(
        "--trace-python-allocations",
        action="store_true",
        help=(
            "Enable tracemalloc in the child. Run separately because tracing "
            "adds material CPU and memory overhead."
        ),
    )
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
    parser.add_argument(
        "--compare-to",
        type=Path,
        help="Optional prior benchmark JSON to compare against.",
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
    report: dict[str, object] = {
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
        "result_schema_version": 2,
        "revision": revision,
        "runs": list(results),
        "summary": summarize(results),
        "worktree_dirty": worktree_dirty,
    }
    if arguments.trace_python_allocations:
        command = report["command"]
        assert isinstance(command, dict)
        command["trace_python_allocations"] = True
    hashes = results[0].get("hashes")
    if isinstance(hashes, dict):
        report["semantic_hashes"] = hashes
    if arguments.compare_to is not None:
        report["comparison"] = compare_reports(
            _load_report(arguments.compare_to),
            report,
        )
    return report


def summarize(results: Iterable[dict[str, object]]) -> dict[str, object]:
    """Return medians without hiding the individual fresh-process evidence."""

    items = tuple(results)
    if not items:
        raise PreparationBenchmarkError("At least one benchmark result is required")
    summary: dict[str, object] = {
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
    optional_metrics = {
        "median_cpu_system_seconds": lambda item: item.get(
            "cpu_system_seconds"
        ),
        "median_cpu_user_seconds": lambda item: item.get("cpu_user_seconds"),
        "median_database_used_mib": lambda item: (
            dict(item["storage"])["database_used_bytes"] / (1024 * 1024)
            if isinstance(item.get("storage"), dict)
            else None
        ),
        "median_peak_process_tree_mib": lambda item: item.get(
            "peak_process_tree_mib"
        ),
        "median_project_storage_mib": lambda item: (
            dict(item["storage"])["project_storage_bytes"] / (1024 * 1024)
            if isinstance(item.get("storage"), dict)
            else None
        ),
    }
    for name, value_for in optional_metrics.items():
        values = tuple(value_for(item) for item in items)
        if all(value is not None for value in values):
            summary[name] = statistics.median(float(value) for value in values)
    return summary


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
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in SUPPORTED_CHILD_SCHEMAS
    ):
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
            "IMPODO_PREPARATION_TRACE_PYTHON": (
                "1" if arguments.trace_python_allocations else "0"
            ),
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
        if int(first.get("schema_version", 0)) >= 2 and (
            result.get("hashes") != first.get("hashes")
            or result.get("counts") != first.get("counts")
        ):
            raise PreparationBenchmarkError(
                "Fresh-process benchmark semantic evidence is not identical"
            )


def compare_reports(
    baseline: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    """Compare same-fixture medians with positive percentages for gains."""

    baseline_command = baseline.get("command")
    candidate_command = candidate.get("command")
    if baseline_command != candidate_command:
        raise PreparationBenchmarkError(
            "Benchmark comparison requires identical workload arguments"
        )
    baseline_runs = baseline.get("runs")
    candidate_runs = candidate.get("runs")
    if (
        not isinstance(baseline_runs, list)
        or not baseline_runs
        or not isinstance(candidate_runs, list)
        or not candidate_runs
    ):
        raise PreparationBenchmarkError("Benchmark comparison runs are missing")
    first_before = baseline_runs[0]
    first_after = candidate_runs[0]
    for key in ("platform", "runtime_versions"):
        if first_before.get(key) != first_after.get(key):
            raise PreparationBenchmarkError(
                "Benchmark comparison requires the same platform and runtime"
            )
    before_fixture = first_before.get("fixture")
    after_fixture = first_after.get("fixture")
    if not isinstance(before_fixture, dict) or not isinstance(after_fixture, dict):
        raise PreparationBenchmarkError("Benchmark comparison fixture is missing")
    if any(
        before_fixture.get(key) != after_fixture.get(key)
        for key in ("sha256", "size_bytes")
    ):
        raise PreparationBenchmarkError(
            "Benchmark comparison requires a byte-identical fixture"
        )
    baseline_summary = baseline.get("summary")
    candidate_summary = candidate.get("summary")
    if not isinstance(baseline_summary, dict) or not isinstance(
        candidate_summary, dict
    ):
        raise PreparationBenchmarkError("Benchmark comparison summary is missing")
    metric_names = tuple(
        sorted(
            key
            for key in baseline_summary.keys() & candidate_summary.keys()
            if key.startswith("median_")
        )
    )
    metrics: dict[str, object] = {}
    for name in metric_names:
        before = float(baseline_summary[name])
        after = float(candidate_summary[name])
        metrics[name] = {
            "absolute_change": after - before,
            "baseline": before,
            "candidate": after,
            "gain_percent": (
                ((before - after) / before) * 100 if before else None
            ),
        }
    return {
        "baseline_revision": str(baseline.get("revision", "unknown")),
        "candidate_revision": str(candidate.get("revision", "unknown")),
        "metrics": metrics,
    }


def _load_report(path: Path) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PreparationBenchmarkError("Benchmark comparison file was not found")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PreparationBenchmarkError(
            "Benchmark comparison file is invalid"
        ) from error
    if not isinstance(payload, dict):
        raise PreparationBenchmarkError("Benchmark comparison file is invalid")
    return payload


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
    compare_to = getattr(arguments, "compare_to", None)
    if compare_to is not None and not (
        compare_to.expanduser().resolve().is_file()
    ):
        raise PreparationBenchmarkError(
            "Benchmark comparison file was not found"
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
