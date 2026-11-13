"""Measure bounded Stage 5 reads through a fictional JSON-2 transport.

This benchmark contacts no Odoo server and reads no running application store.
It measures the real connector with injected network latency. "Cold" means a
new connector; "repeat" reuses its fingerprint. Neither means a cold OS cache.
Diagnostics cohorts measure collector setup, reads, terminal capture and log
close together. Recorder construction and temporary-directory allocation are
outside the timed segment, matching an already-running application recorder.
Use paired measurements on a quiet host; this is not Windows qualification.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter, process_time, sleep
from typing import Any, Self
from uuid import uuid4

import psutil

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.application.shared.build_contract import (
    PROCESS_BUILD_CONTRACT,
    calculate_application_build_contract,
    require_same_application_build,
)
from impodo.application.shared.performance import (
    OperationPerformance,
    bind_performance,
    performance_phase,
)
from impodo.domain.odoo.contracts import RecordRequest, record_snapshot_payload
from impodo.domain.shared.models import canonical_json_bytes
from impodo.web.diagnostics import LocalDiagnosticRecorder

FIXED_NOW = datetime(2026, 11, 13, tzinfo=UTC)
MODELS = ("x.fixture_component", "x.fixture_resource")
FIELDS = ("code", "amount", "enabled")


@dataclass(frozen=True)
class ReadFixture:
    """Define the identical fictional workload for all worker counts."""

    batches: int = 12
    rows_per_batch: int = 200
    page_size: int = 100
    latency_ms: float = 10.0

    def __post_init__(self) -> None:
        if min(self.batches, self.rows_per_batch, self.page_size) < 1:
            raise ValueError("Batch, row and page counts must be positive")
        if self.batches > 1_000 or self.rows_per_batch > 100_000:
            raise ValueError("Fictional fixture exceeds its bounded size")
        if not math.isfinite(self.latency_ms) or not 0 <= self.latency_ms <= 1_000:
            raise ValueError("Injected latency must be between 0 and 1000 ms")

    def requests(self) -> tuple[RecordRequest, ...]:
        """Return independent requests with deterministic same-model chunks."""

        return tuple(
            RecordRequest(MODELS[batch % len(MODELS)], FIELDS, (("batch", "=", batch),))
            for batch in range(self.batches)
        )


class FictionalTransport:
    """Inject latency while checking bounded, sequential pages per request."""

    def __init__(self, fixture: ReadFixture, major: int) -> None:
        if major not in {19, 20}:
            raise ValueError("Only Odoo 19 and 20 are supported")
        self.fixture = fixture
        self.major = major
        self._lock = Lock()
        self._active: set[tuple[str, int]] = set()
        self.reset()

    def reset(self) -> None:
        """Start counters only after every task of the previous run ended."""

        with self._lock:
            if self._active:
                raise AssertionError("A previous benchmark task is still running")
            self.version_calls = 0
            self.page_calls = 0
            self.peak_in_flight = 0
            self.offsets: dict[tuple[str, int], list[int]] = {}

    def __call__(
        self,
        url: str,
        _headers: Mapping[str, str],
        body: bytes | None,
        _timeout: float,
        _method: str,
    ) -> tuple[int, Any]:
        if url.endswith("/web/version"):
            with self._lock:
                self.version_calls += 1
            return 200, {"version": f"{self.major}.0"}
        model = url.rsplit("/", 2)[-2]
        if model not in MODELS or not url.endswith("/search_read") or body is None:
            raise AssertionError("Benchmark received an unplanned read")
        payload = json.loads(body)
        domain = payload["domain"]
        if len(domain) != 1 or domain[0][:2] != ["batch", "="]:
            raise AssertionError("Benchmark request widened its domain")
        batch = domain[0][2]
        if type(batch) is not int or not 0 <= batch < self.fixture.batches:
            raise AssertionError("Benchmark request changed its batch")
        if model != MODELS[batch % len(MODELS)]:
            raise AssertionError("Benchmark request changed its model")
        if payload["order"] != "id asc" or tuple(payload["fields"]) != ("id", *FIELDS):
            raise AssertionError("Benchmark request changed its projection")
        offset, limit = payload["offset"], payload["limit"]
        key = (model, batch)
        with self._lock:
            if key in self._active:
                raise AssertionError("Pages within one request overlapped")
            self._active.add(key)
            self.page_calls += 1
            self.peak_in_flight = max(self.peak_in_flight, len(self._active))
            self.offsets.setdefault(key, []).append(offset)
        try:
            sleep(self.fixture.latency_ms / 1_000)
            return 200, [
                {
                    "id": batch * self.fixture.rows_per_batch + ordinal + 1,
                    "code": f"FIX-{batch:04d}-{ordinal:06d}",
                    "amount": ordinal % 17,
                    "enabled": ordinal % 2 == 0,
                }
                for ordinal in range(
                    offset, min(offset + limit, self.fixture.rows_per_batch)
                )
            ]
        finally:
            with self._lock:
                self._active.remove(key)

    def assert_complete(self, workers: int) -> None:
        """Require every page once, in order, and a bounded in-flight count."""

        expected = list(
            range(0, self.fixture.rows_per_batch + 1, self.fixture.page_size)
        )
        if len(self.offsets) != self.fixture.batches:
            raise AssertionError("Benchmark omitted a batch")
        if any(offsets != expected for offsets in self.offsets.values()):
            raise AssertionError("Benchmark omitted, repeated or reordered a page")
        if self.peak_in_flight > workers or self._active:
            raise AssertionError("Benchmark exceeded its concurrency bound")


class _RssSampler:
    """Observe process-wide RSS at low frequency; this is not thread memory."""

    def __init__(self) -> None:
        self._process = psutil.Process()
        self._stop = Event()
        self._thread = Thread(target=self._sample_loop, daemon=True)
        self.start_bytes = self._process.memory_info().rss
        self.peak_bytes = self.start_bytes

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)

    def _sample_loop(self) -> None:
        while not self._stop.wait(0.02):
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)


def _connector(
    fixture: ReadFixture, transport: FictionalTransport
) -> Json2ReadConnector:
    return Json2ReadConnector(
        Json2Config(
            base_url="https://fixture.example.test",
            database="fictional_stage5",
            api_key="fictional-not-a-credential",
            page_size=fixture.page_size,
            retries=0,
            context={"allowed_company_ids": [1], "active_test": False},
        ),
        transport=transport,
        now=lambda: FIXED_NOW,
    )


def measure_read(
    connector: Json2ReadConnector,
    transport: FictionalTransport,
    *,
    workers: int,
    temperature: str,
    sample: int,
    diagnostics_mode: str = "off",
) -> dict[str, object]:
    """Retain one timed connector result after verifying complete evidence."""

    if diagnostics_mode not in {"off", "on"}:
        raise ValueError("One measurement requires diagnostics off or on")
    transport.reset()
    if diagnostics_mode == "on":
        trial_parent = Path(tempfile.gettempdir()) / "impodo-trials"
        trial_parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(
            prefix="trial.", dir=trial_parent
        )
    else:
        temporary = nullcontext(None)
    with temporary as directory:
        recorder = LocalDiagnosticRecorder(directory) if directory is not None else None
        with _RssSampler() as memory:
            wall_started, cpu_started = perf_counter(), process_time()
            try:
                collector = (
                    OperationPerformance(
                        operation_id=str(uuid4()),
                        operation="preflight",
                        configured_workers=workers,
                        sink=recorder.record_performance,
                    )
                    if recorder is not None
                    else None
                )
                with bind_performance(collector), performance_phase("read_records"):
                    snapshot = connector.get_records(
                        transport.fixture.requests(), max_workers=workers
                    )
                if collector is not None:
                    collector.finish(outcome="succeeded")
            finally:
                if recorder is not None:
                    recorder.close()
            cpu_seconds, wall_seconds = (
                process_time() - cpu_started,
                perf_counter() - wall_started,
            )
        diagnostic_evidence = (
            _diagnostic_evidence(Path(directory))
            if directory is not None
            else {"diagnostic_record_count": 0, "diagnostic_log_bytes": 0}
        )
    transport.assert_complete(workers)
    payload = record_snapshot_payload(snapshot)
    semantic_hash = "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()
    rows = sum(len(records) for records in snapshot.records.values())
    if rows != transport.fixture.batches * transport.fixture.rows_per_batch:
        raise AssertionError("Benchmark returned the wrong row count")
    for records in snapshot.records.values():
        if [record.odoo_id for record in records] != sorted(
            record.odoo_id for record in records
        ):
            raise AssertionError("Benchmark changed canonical record order")
    return {
        "major": transport.major,
        "workers": workers,
        "temperature": temperature,
        "sample": sample,
        "diagnostics_mode": diagnostics_mode,
        "wall_seconds": wall_seconds,
        "process_cpu_seconds": cpu_seconds,
        "process_start_rss_bytes": memory.start_bytes,
        "process_peak_observed_rss_bytes": memory.peak_bytes,
        "version_calls": transport.version_calls,
        "record_page_calls": transport.page_calls,
        "peak_in_flight_reads": transport.peak_in_flight,
        "rows": rows,
        "semantic_hash": semantic_hash,
        **diagnostic_evidence,
    }


def _diagnostic_evidence(directory: Path) -> dict[str, object]:
    """Require real captured phases and one terminal fact, outside timing."""

    payloads = [
        path.read_bytes()
        for path in sorted(directory.glob("impodo.jsonl*"))
        if path.is_file()
    ]
    records = [
        json.loads(line)
        for payload in payloads
        for line in payload.splitlines()
        if line
    ]
    terminal = [
        record
        for record in records
        if record.get("event") == "performance_operation_completed"
    ]
    if len(terminal) != 1 or terminal[0].get("outcome") != "succeeded":
        raise AssertionError(
            "Diagnostics cohort did not capture one successful terminal fact"
        )
    if terminal[0].get("capture_failure_count") != 0:
        raise AssertionError("Diagnostic capture failed during overhead measurement")
    if "read_records" not in terminal[0].get("phases", {}):
        raise AssertionError("Diagnostics cohort omitted the measured read phase")
    return {
        "diagnostic_record_count": len(records),
        "diagnostic_log_bytes": sum(len(payload) for payload in payloads),
        "diagnostic_counters": terminal[0].get("counters", {}),
        "diagnostic_observed_workers": terminal[0].get("observed_workers"),
    }


def summarize_reads(runs: Sequence[dict[str, Any]]) -> list[dict[str, object]]:
    """Keep raw samples separately and reject changed results or call counts."""

    summaries: list[dict[str, object]] = []
    groups = sorted(
        {
            (run["major"], run["temperature"], run["workers"], run["diagnostics_mode"])
            for run in runs
        }
    )
    for major in sorted({run["major"] for run in runs}):
        same_major = [run for run in runs if run["major"] == major]
        for name in ("semantic_hash", "record_page_calls", "rows"):
            if len({run[name] for run in same_major}) != 1:
                raise AssertionError(f"Worker modes produced different {name}")
    for major, temperature, workers, diagnostics_mode in groups:
        selected = [
            run
            for run in runs
            if (
                run["major"],
                run["temperature"],
                run["workers"],
                run["diagnostics_mode"],
            )
            == (major, temperature, workers, diagnostics_mode)
        ]
        summaries.append(
            {
                "major": major,
                "temperature": temperature,
                "workers": workers,
                "diagnostics_mode": diagnostics_mode,
                "samples": len(selected),
                "median_wall_seconds": statistics.median(
                    run["wall_seconds"] for run in selected
                ),
                "median_process_cpu_seconds": statistics.median(
                    run["process_cpu_seconds"] for run in selected
                ),
                "peak_process_observed_rss_bytes": max(
                    run["process_peak_observed_rss_bytes"] for run in selected
                ),
                "peak_in_flight_reads": max(
                    run["peak_in_flight_reads"] for run in selected
                ),
                "semantic_hash": selected[0]["semantic_hash"],
                "record_page_calls": selected[0]["record_page_calls"],
                "median_diagnostic_log_bytes": statistics.median(
                    run["diagnostic_log_bytes"] for run in selected
                ),
            }
        )
    return summaries


def run_benchmark(
    fixture: ReadFixture,
    *,
    samples: int = 3,
    workers: tuple[int, ...] = (1, 2),
    majors: tuple[int, ...] = (19, 20),
    diagnostics_mode: str = "off",
) -> dict[str, object]:
    """Alternate worker modes and preserve every cold/repeat observation."""

    if samples < 1 or not workers or not majors:
        raise ValueError("At least one sample, worker mode and major are required")
    if any(type(worker) is not int or worker not in {1, 2, 3, 4} for worker in workers):
        raise ValueError("Worker modes must be integers from 1 to 4")
    if len(set(workers)) != len(workers) or len(set(majors)) != len(majors):
        raise ValueError("Worker modes and Odoo majors must not repeat")
    if diagnostics_mode not in {"off", "on", "both"}:
        raise ValueError("Diagnostics mode must be off, on or both")
    require_same_application_build(
        PROCESS_BUILD_CONTRACT, calculate_application_build_contract()
    )
    harness_hash = sha256(Path(__file__).read_bytes()).hexdigest()
    runs = []
    for major in majors:
        for sample in range(samples):
            order = workers if sample % 2 == 0 else tuple(reversed(workers))
            diagnostic_cohorts = (
                ("off", "on") if diagnostics_mode == "both" else (diagnostics_mode,)
            )
            if sample % 2:
                diagnostic_cohorts = tuple(reversed(diagnostic_cohorts))
            for worker in order:
                for diagnostic_cohort in diagnostic_cohorts:
                    transport = FictionalTransport(fixture, major)
                    connector = _connector(fixture, transport)
                    for temperature in ("cold_connector", "repeat_connector"):
                        runs.append(
                            measure_read(
                                connector,
                                transport,
                                workers=worker,
                                temperature=temperature,
                                sample=sample,
                                diagnostics_mode=diagnostic_cohort,
                            )
                        )
    require_same_application_build(
        PROCESS_BUILD_CONTRACT, calculate_application_build_contract()
    )
    if harness_hash != sha256(Path(__file__).read_bytes()).hexdigest():
        raise RuntimeError(
            "The benchmark changed during measurement; discard these runs"
        )
    return {
        "schema_version": 1,
        "scope": "controlled fictional JSON-2 record reads; no live Odoo or Windows qualification",
        "timed_segment": "operation collector setup, connector read, phase/terminal capture and recorder close; recorder/temp-directory setup and evidence verification excluded",
        "diagnostics_mode": diagnostics_mode,
        "clock": "fixed fingerprint timestamp; wall time uses perf_counter",
        "memory_scope": "sampled process-wide RSS; 20 ms interval; peak is an observation",
        "runtime": {
            "operating_system": platform.system(),
            "operating_system_release": platform.release(),
            "architecture": platform.machine(),
            "python": sys.version.split()[0],
            "logical_cpus": psutil.cpu_count(),
            "total_ram_bytes": psutil.virtual_memory().total,
            "dependency_versions": {
                name: version(name) for name in ("duckdb", "polars", "psutil")
            },
        },
        "fixture": asdict(fixture),
        "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "harness_hash": "sha256:" + harness_hash,
        "fixture_hash": "sha256:"
        + sha256(canonical_json_bytes(asdict(fixture))).hexdigest(),
        "raw_runs": runs,
        "summaries": summarize_reads(runs),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--batches", type=int, default=12)
    parser.add_argument("--rows-per-batch", type=int, default=200)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--latency-ms", type=float, default=10.0)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2])
    parser.add_argument(
        "--diagnostics-mode", choices=("off", "on", "both"), default="off"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmark(
        ReadFixture(args.batches, args.rows_per_batch, args.page_size, args.latency_ms),
        samples=args.samples,
        workers=tuple(args.workers),
        diagnostics_mode=args.diagnostics_mode,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
