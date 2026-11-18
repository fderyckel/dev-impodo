"""Temporary Plan 09 collector for the isolated Windows diagnostic kit."""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import gmtime, strftime
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs/developer/reference/workflow-liveness-macos.json"
SUPERVISOR_CASES = (
    "test_windows_server_uses_selector_event_loop",
    "test_listener_check_rejects_bound_and_closed_sockets",
    "test_console_interrupt_allows_child_to_finish_gracefully",
    "test_console_interrupt_terminates_child_after_grace_period",
    "test_second_unexpected_exit_opens_the_restart_circuit",
    "test_spawned_server_reuses_listener_and_session_after_restart",
    "test_unexpected_exit_restarts_once_on_the_same_listener",
)
LAUNCHER_CASES = (
    "test_launcher_records_exception_class_without_exception_message",
    "test_launcher_records_normal_start_and_stop_without_the_launch_token",
)
SCALE_TEST = "tests.performance.test_preflight_scale.DurablePreflightScaleTests.test_durable_preflight_workflow"


def save_bundle(output, summary, *, base=b"", samples=None):
    """Keep a returnable ZIP after each stage, including an unsuccessful setup."""
    buffer = io.BytesIO(base)
    with ZipFile(buffer, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr("windows-qualification.json", json.dumps(summary, indent=2))
        if samples is not None:
            archive.writestr("workflow-liveness.json", samples)
    pending = output.with_suffix(".tmp")
    pending.write_bytes(buffer.getvalue())
    pending.replace(output)


def recovery_checks():
    cases = {}
    for group, names in (
        ("ServerSupervisorTests", SUPERVISOR_CASES),
        ("LauncherDiagnosticTests", LAUNCHER_CASES),
    ):
        for name in names:
            selector = f"tests.integration.web.test_diagnostics.{group}.{name}"
            suite = unittest.defaultTestLoader.loadTestsFromName(selector)
            result = unittest.TestResult()
            suite.run(result)
            if result.errors:
                outcome = "error"
            elif result.failures:
                outcome = "failed"
            elif result.skipped or result.testsRun != 1:
                outcome = "unavailable"
            else:
                outcome = "passed"
            cases[name] = outcome
    return cases


def assess(report, baseline, scale_ok, system):
    """Separate a measured budget miss from missing or mismatched evidence."""
    differences = [
        key
        for key in (
            "build",
            "python",
            "duckdb",
            "versions",
            "rows",
            "cold_runs_per_page",
            "warm_reads_per_process",
        )
        if report.get(key) != baseline[key]
    ]
    for key in ("source_hash", "mapping_hash", "unchanged", "columns", "mapped_fields"):
        if report.get("fixture", {}).get(key) != baseline["fixture"][key]:
            differences.append(key)
    if system != "Windows":
        differences.append("operating_system")
    if not report or report.get("status") == "incomplete":
        return "incomplete", differences
    if differences or not report.get("representative"):
        return "inconclusive", differences
    runs = report.get("runs", [])
    for page in ("overview", "mapping", "review", "changes"):
        cases = [row for row in runs if row["page"] == page]
        if len(cases) != 20 or any(len(row["warm"]) != 5 for row in cases):
            return "incomplete", differences
    if report.get("status") == "over_budget":
        return "over_budget", differences
    if scale_ok and report.get("status") == "passed":
        return "passed", differences
    return "incomplete", differences


def collect(work, output):
    from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
    from impodo.web.diagnostics import create_diagnostic_bundle

    summary = json.loads(read_summary(output))
    # Only this run's private, empty log directory is supplied to the normal exporter.
    diagnostics = work / "diagnostics"
    diagnostics.mkdir()
    base = create_diagnostic_bundle(diagnostics, build_contract=PROCESS_BUILD_CONTRACT)
    summary["stage"] = "checking_launcher"
    save_bundle(output, summary, base=base)
    print("1 of 2: checking the local launcher and restart...", flush=True)
    summary["launcher_recovery"] = recovery_checks()
    summary["stage"] = "measuring_pages"
    save_bundle(output, summary, base=base)
    print(
        "2 of 2: measuring four pages in 80 fresh processes. Keep the laptop awake.",
        flush=True,
    )
    raw = work / "workflow-liveness.json"
    os.environ.update(
        IMPODO_RUN_PREFLIGHT_SCALE="1",
        IMPODO_PREFLIGHT_SCALE_ROWS="25000",
        IMPODO_PREFLIGHT_SCALE_RUNS="1",
        IMPODO_WORKFLOW_LIVENESS_RUNS="20",
        IMPODO_PREPARATION_SCALE_WORKLOAD="products",
        IMPODO_WORKFLOW_LIVENESS_OUTPUT=str(raw),
    )
    result = unittest.TestResult()
    interrupted = False
    try:
        with (
            open(os.devnull, "w", encoding="utf-8") as quiet,
            contextlib.redirect_stdout(quiet),
        ):
            unittest.defaultTestLoader.loadTestsFromName(SCALE_TEST).run(result)
    except KeyboardInterrupt:
        interrupted = True
    baseline = json.loads(REFERENCE.read_bytes())
    samples = raw.read_bytes() if raw.is_file() else None
    report = json.loads(samples) if samples else {}
    scale_ok = (
        not interrupted
        and result.testsRun == 1
        and result.wasSuccessful()
        and not result.skipped
    )
    verdict, differences = assess(report, baseline, scale_ok, platform.system())
    summary.update(
        stage="interrupted" if interrupted else "finished",
        performance=verdict,
        differing_inputs=differences,
        reference_build=baseline["build"],
        scale_test_passed=bool(scale_ok),
    )
    if verdict in {"passed", "over_budget"}:
        summary["passed"] = verdict == "passed" and all(
            outcome == "passed" for outcome in summary["launcher_recovery"].values()
        )
    save_bundle(output, summary, base=base, samples=samples)


def read_summary(output):
    with ZipFile(output) as archive:
        return archive.read("windows-qualification.json")


def find_uv(work):
    for runner in (
        ROOT / ".tools/uv/uv.exe",
        ROOT / ".tools/uv.exe",
        Path.home() / "Applications/dev-impodo/.tools/uv.exe",
    ):
        if runner.is_file():
            return str(runner)
    installed = shutil.which("uv")
    if installed:
        return installed
    # The bootstrap lives only in the temporary trial directory.
    bootstrap = work / "bootstrap"
    subprocess.run([sys.executable, "-m", "venv", str(bootstrap)], check=True)
    binaries = bootstrap / ("Scripts" if os.name == "nt" else "bin")
    python = binaries / ("python.exe" if os.name == "nt" else "python")
    subprocess.run([str(python), "-m", "pip", "install", "uv==0.13.4"], check=True)
    return str(binaries / ("uv.exe" if os.name == "nt" else "uv"))


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--collect":
        collect(Path(sys.argv[2]), Path(sys.argv[3]))
        return 0

    attempt = uuid4()
    output = ROOT / f"impodo-windows-diagnostic-{attempt.hex[:8]}.zip"
    summary = {
        "schema_version": 1,
        "scope": "isolated_plan_09_fixture",
        "attempt_id": str(attempt),
        "created_at": strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()),
        "system": platform.system(),
        "stage": "setup",
        "performance": "not_run",
        "launcher_recovery": {},
        "passed": None,
    }
    save_bundle(output, summary)
    print(f"Return this file even if a check fails: {output}", flush=True)
    parent = Path(tempfile.gettempdir()) / "impodo-trials"
    parent.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="trial.", dir=parent) as directory:
            work = Path(directory)
            env = {k: v for k, v in os.environ.items() if not k.startswith("IMPODO_")}
            env.update(
                TMP=str(work),
                TEMP=str(work),
                TMPDIR=str(work),
                UV_PROJECT_ENVIRONMENT=str(work / "venv"),
                PYTHONNOUSERSITE="1",
            )
            env.pop("PYTHONPATH", None)
            command = [
                find_uv(work),
                "run",
                "--frozen",
                "--no-default-groups",
                "--extra",
                "test",
                "--python",
                "3.12.14",
                "python",
                "-m",
                "scripts.windows_workflow_diagnostic",
                "--collect",
                str(work),
                str(output),
            ]
            subprocess.run(command, cwd=ROOT, env=env, check=True)
    except (KeyboardInterrupt, OSError, subprocess.SubprocessError) as error:
        print(
            f"Collection stopped ({type(error).__name__}). Return the ZIP shown above.",
            flush=True,
        )
        return 1
    print(f"Finished. Return {output.name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
