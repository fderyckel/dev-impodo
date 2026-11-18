"""Measure authenticated page reads and overlapping polls on one ASGI event loop."""

from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from threading import Event
from time import perf_counter, process_time
from unittest.mock import patch
from uuid import uuid4

import duckdb
import psutil
from fastapi.testclient import TestClient

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.preflight_service import EXECUTION_SNAPSHOT_NAME, MANIFEST_NAME
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.execution.load_jobs import LoadJobResult
from impodo.domain.shared.access import Capability
from impodo.web.app import create_local_app
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    REQUEST_ID_HEADER,
    LocalDiagnosticRecorder,
)
from tests.support.identity import resolve_local_actor
from tests.support.paths import REPOSITORY_ROOT
from tests.support.workflow_database_probe import (
    instrument_stores,
    observe_store_work,
)

PAGES = {
    "overview": "overview",
    "mapping": "mapping",
    "review": "summary?step=results",
    "changes": "load/review",
}


@contextmanager
def running_status(context):
    """Hold a synthetic sibling job in the real manager without performing any Odoo call."""

    actor = resolve_local_actor(context)
    peer = context.project_authoring.create(
        actor=actor,
        display_name="Polling fixture",
        source_mode="FILE",
        creation_request_id=str(uuid4()),
        source_system_identity="Fictional source",
    ).workspace_state
    access = context.workspace_access.resolve(
        peer.workspace_id, actor=actor, capability=Capability.PROJECT_VIEW
    )
    started, release = Event(), Event()

    def wait_for_probes(_access, _progress, _verification):
        started.set()
        if not release.wait(180):
            raise TimeoutError("The page probe exceeded its deadline")
        return LoadJobResult(str(uuid4()), False)

    job = context.load_jobs.enqueue(
        peer.workspace_id,
        peer.name,
        target_database="fixture",
        target_server="fixture",
        target_environment="fixture",
        total_rows=0,
        access_context=access,
        actor=actor,
        work=wait_for_probes,
    )
    try:
        if not started.wait(15):
            raise AssertionError("Synthetic job did not start")
        yield f"/workspaces/{peer.workspace_id}/load/progress/{job.job_id}/status"
    finally:
        release.set()


class PageObservation:
    """Bind counters only to the measured page and wake probe clients at its first physical open."""

    def __init__(self, app, page):
        self.app = app
        self.path = page.partition("?")[0]
        self.started = Event()
        self.finished = Event()
        self.finished_at = 0
        self.stores = None
        self.queue_ms = None

    async def __call__(self, scope, receive, send):
        if scope.get("path") != self.path:
            return await self.app(scope, receive, send)
        with observe_store_work(self.started) as self.stores:
            try:
                await self.app(scope, receive, send)
            finally:
                self.finished_at = perf_counter()
                self.finished.set()
                self.queue_ms = scope.get("state", {}).get(
                    "workspace_page_queue_wait_ms"
                )


def page_sample(client, observation, page, status, pool):
    process = psutil.Process()
    observation.started.clear()
    observation.finished.clear()
    entry = [Event(), Event()]

    def probe(path, ready):
        ready.set()
        if not observation.started.wait(15):
            raise AssertionError("Page construction never opened a store")
        samples = []
        while True:
            started = perf_counter()
            if observation.finished.is_set():
                break
            response = client.get(path, follow_redirects=False)
            elapsed = (perf_counter() - started) * 1000
            response.raise_for_status()
            assert response.json()["status"] == (
                "ok" if path == "/health" else "RUNNING"
            )
            samples.append((started, elapsed))
            observation.finished.wait(0.025)
        return samples

    probes = [
        pool.submit(probe, path, ready)
        for path, ready in zip(("/health", status), entry, strict=True)
    ]
    assert all(ready.wait(10) for ready in entry)
    before_rss = process.memory_info().rss
    started, cpu = perf_counter(), process_time()
    response = client.get(page, follow_redirects=False)
    elapsed = (perf_counter() - started) * 1000
    cpu_ms = (process_time() - cpu) * 1000
    rss_growth = process.memory_info().rss - before_rss
    assert response.status_code == 200, (page, response.status_code)
    measurements = {}
    for name, future in zip(("health", "active_status"), probes, strict=True):
        polls = future.result(timeout=10)
        assert polls and all(
            started <= at <= observation.finished_at for at, _ in polls
        ), "Poll did not overlap the page"
        measurements[name + "_ms"] = [ms for _, ms in polls]
    return {
        "request_id": response.headers[REQUEST_ID_HEADER],
        "elapsed_ms": elapsed,
        "cpu_ms": cpu_ms,
        "rss_growth_bytes": rss_growth,
        "queue_wait_ms": observation.queue_ms,
        "physical_opens": dict(observation.stores.opens),
        "schema_checks": dict(observation.stores.schemas),
        **measurements,
    }


def worker(root, workspace_id, name, warm):
    directory = root.parent / "liveness-diagnostics"
    recorder = LocalDiagnosticRecorder(directory)
    app = create_local_app(
        root,
        launch_token="fixture",
        session_secret="fixture-secret",
        diagnostic_recorder=recorder,
        secret_store=MemorySecretStore(),
        preflight_jobs_enabled=True,
    )
    page = f"/workspaces/{workspace_id}/{PAGES[name]}"
    observed = PageObservation(app, page)
    materialize = app.state.context.artifacts.materialize_report

    def bounded_report(workspace_id, run_id, filename):
        if filename in (EXECUTION_SNAPSHOT_NAME, MANIFEST_NAME):
            raise AssertionError("Page materialized full comparison evidence")
        return materialize(workspace_id, run_id, filename)

    try:
        with (
            TestClient(observed) as client,
            running_status(app.state.context) as status,
            instrument_stores(),
            patch.object(
                app.state.context.artifacts, "materialize_report", bounded_report
            ),
            ThreadPoolExecutor(2) as pool,
        ):
            assert (
                client.get("/launch?token=fixture", follow_redirects=False).status_code
                == 303
            )
            samples = [
                page_sample(client, observed, page, status, pool)
                for _ in range(warm + 1)
            ]
    finally:
        recorder.close()
    records = [
        json.loads(line)
        for line in (directory / DIAGNOSTIC_LOG_NAME).read_text().splitlines()
    ]
    requests = {
        r["request_id"]: r for r in records if r["event"] == "request_completed"
    }
    for sample in samples:
        diagnostic = requests[sample.pop("request_id")]
        assert (
            sum(sample["physical_opens"].values())
            == diagnostic["database_connection_count"]
        )
        assert (
            sum(sample["schema_checks"].values())
            == diagnostic["database_schema_check_count"]
        )
        if name == "overview":
            assert sample["physical_opens"]["registry.duckdb"] == 1
            assert sample["physical_opens"]["workspace-engine.duckdb"] == 1
        sample["full_comparison_artifact_bytes_read"] = 0
        sample["server_timings_ms"] = diagnostic.get("server_timings_ms", {})
        sample["file_io"] = diagnostic.get("file_io", {"status": "unavailable"})
    return {
        "page": name,
        "build": PROCESS_BUILD_CONTRACT.application_build_id,
        "cold": samples[0],
        "warm": samples[1:],
    }


def p95(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def qualify(root, workspace_id, rows, fixture, output, *, runs, warm=5):
    """Start each cold page in a separate process against the same saved mature fixture."""

    if runs < 1 or warm < 1:
        raise ValueError("Cold and warm run counts must both be positive")
    report = {
        "rows": rows,
        "fixture": fixture,
        "cold_runs_per_page": runs,
        "warm_reads_per_process": warm,
        "representative": rows >= 25000 and runs >= 20,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "duckdb": duckdb.__version__,
        "versions": {name: version(name) for name in ("fastapi", "starlette", "anyio", "psutil", "polars")},
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "memory_bytes": psutil.virtual_memory().total,
        "build": PROCESS_BUILD_CONTRACT.application_build_id,
        "runs": [],
        "status": "incomplete",
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        pending = output.with_suffix(output.suffix + ".tmp")
        pending.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        pending.replace(output)

    save()
    for number in range(1, runs + 1):
        for name in PAGES:
            child_root = root.parent / "liveness-child" / "project-data"
            try:
                shutil.copytree(root, child_root)
                child = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.performance.workflow_liveness",
                        str(child_root),
                        workspace_id,
                        name,
                        str(warm),
                    ],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=240,
                )
                if child.returncode:
                    raise AssertionError(
                        child.stderr[-3000:] or "Child diagnostics unavailable"
                    )
                result = json.loads(child.stdout.strip().splitlines()[-1])
                if result["build"] != report["build"]:
                    raise AssertionError(
                        "Application build changed during qualification"
                    )
                report["runs"].append({"run": number, **result})
                save()
            except (
                OSError,
                ValueError,
                AssertionError,
                subprocess.SubprocessError,
            ) as error:
                report["failed_child"] = {
                    "page": name,
                    "run": number,
                    "reason": type(error).__name__,
                }
                save()
                raise
            finally:
                if child_root.parent.exists():
                    shutil.rmtree(child_root.parent)
    summary = {}
    for name in PAGES:
        cases = [case for case in report["runs"] if case["page"] == name]
        cold = [case["cold"] for case in cases]
        repeats = [sample for case in cases for sample in case["warm"]]
        summary[name] = {
            "cold_p95_ms": p95([s["elapsed_ms"] for s in cold]),
            "warm_p95_ms": p95([s["elapsed_ms"] for s in repeats]),
        }
        for probe in ("health_ms", "active_status_ms"):
            values = [ms for s in cold + repeats for ms in s[probe]]
            summary[name][probe + "_p95"] = p95(values)
            summary[name][probe + "_max"] = max(values)
    report["summary"] = summary
    passed = (
        summary["overview"]["warm_p95_ms"] <= 1500
        and summary["overview"]["cold_p95_ms"] <= 3000
        and all(
            s[k + "_p95"] <= 250 and s[k + "_max"] < 1000
            for s in summary.values()
            for k in ("health_ms", "active_status_ms")
        )
    )
    report["status"] = "passed" if passed else "over_budget"
    save()
    assert passed, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("workspace_id")
    parser.add_argument("page", choices=PAGES)
    parser.add_argument("warm", type=int)
    args = parser.parse_args()
    print(json.dumps(worker(args.root, args.workspace_id, args.page, args.warm)))
