"""Collect bounded, value-free job costs without changing operation truth.

Each worker owns a collector. Coordinators merge immutable snapshots instead of
sharing mutable context between threads. Wall phases can overlap; thread CPU is
measured on the thread that runs a phase, while memory is process-wide.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter, thread_time
from uuid import UUID

import psutil

PERFORMANCE_SCHEMA_VERSION = 1
PERFORMANCE_PHASES = frozenset(
    {
        "verify",
        "frozen_input",
        "source_package",
        "plan",
        "read_odoo",
        "read_metadata",
        "read_records",
        "read_record_batch",
        "compare",
        "build",
        "publish",
        "finalize",
        "execution_snapshot_serialization",
        "manifest_serialization",
        "manifest_artifact_write",
        "execution_snapshot_artifact_write",
        "report_header_encoding",
        "report_header_insertion",
        "dataset_row_encoding",
        "dataset_row_insertion",
        "decision_row_encoding",
        "decision_row_insertion",
        "target_snapshot_encoding",
        "target_snapshot_insertion",
        "execution_projection_encoding",
        "execution_projection_insertion",
        "current_pointer_update",
        "local_attempt_finalization",
    }
)
PERFORMANCE_COUNTERS = frozenset(
    {
        "rpc_calls",
        "rpc_batches",
        "rpc_pages",
        "rpc_rows",
        "rpc_retries",
        "rpc_failures",
        "rpc_metadata_calls",
        "rpc_record_calls",
        "prepared_rows",
        "target_rows",
        "dataset_count",
        "source_package_reads",
        "artifact_read_bytes",
        "artifact_write_bytes",
        "database_connection_count",
        "database_schema_check_count",
        "database_lock_retry_count",
        "cache_hits",
        "cache_misses",
    }
)
PERFORMANCE_METRICS = frozenset(
    {
        "database_connection_ms",
        "database_schema_ms",
        "database_lock_wait_ms",
    }
)
PerformanceSink = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class PhasePerformance:
    """One aggregate phase; CPU can be absent for external measurements."""

    name: str
    wall_ms: float
    thread_cpu_ms: float | None
    calls: int


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    """Immutable worker costs that can be merged on the coordinator thread."""

    phases: tuple[PhasePerformance, ...]
    counters: tuple[tuple[str, int], ...]
    metrics: tuple[tuple[str, float], ...]
    peak_process_rss_bytes: int | None
    observed_workers: int


class OperationPerformance:
    """Retain fixed-size operational aggregates with a best-effort event sink."""

    def __init__(
        self,
        *,
        operation_id: str,
        operation: str,
        sink: PerformanceSink | None = None,
        configured_workers: int = 1,
        queue_wait_ms: float = 0,
    ) -> None:
        self.operation_id = str(UUID(operation_id))
        if operation not in {"preflight", "preparation"}:
            raise ValueError("Unsupported performance operation")
        self.operation = operation
        self.configured_workers = max(1, min(4, configured_workers))
        self.queue_wait_ms = _number(queue_wait_ms)
        self._sink = sink
        self._started = perf_counter()
        self._cpu_started = thread_time()
        self._phases: dict[str, PhasePerformance] = {}
        self._counters: dict[str, int] = {}
        self._metrics: dict[str, float] = {}
        self._peak_rss: int | None = None
        self._observed_workers = 0
        self._capture_failures = 0
        self._finished = False
        self._sample_memory()
        self._emit(
            {
                "event": "performance_operation_started",
                "queue_wait_ms": self.queue_wait_ms,
                "configured_workers": self.configured_workers,
            }
        )

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Measure a known phase, including failed work, on its owning thread."""
        if name not in PERFORMANCE_PHASES:
            yield
            return
        self._emit({"event": "performance_phase_started", "phase": name})
        wall, cpu = perf_counter(), thread_time()
        try:
            yield
        finally:
            self.record_phase(
                name,
                (perf_counter() - wall) * 1000,
                cpu_ms=(thread_time() - cpu) * 1000,
            )

    def record_phase(
        self,
        name: str,
        wall_ms: float,
        *,
        cpu_ms: float | None = None,
        calls: int = 1,
    ) -> None:
        """Add a known phase and flush a bounded event for interrupted jobs."""
        if name not in PERFORMANCE_PHASES:
            return
        previous = self._phases.get(name)
        cpu = _number(cpu_ms) if cpu_ms is not None else None
        self._phases[name] = PhasePerformance(
            name,
            _number(wall_ms) + (previous.wall_ms if previous else 0),
            (
                cpu + ((previous.thread_cpu_ms or 0) if previous else 0)
                if cpu is not None
                and (previous is None or previous.thread_cpu_ms is not None)
                else None
            ),
            min(1_000_000, max(0, calls) + (previous.calls if previous else 0)),
        )
        self._sample_memory()
        self._emit(
            {
                "event": "performance_phase_completed",
                "phase": name,
                "wall_ms": _number(wall_ms),
                "thread_cpu_ms": cpu,
                "calls": max(0, min(1_000_000, calls)),
                "process_rss_bytes": self._peak_rss,
            }
        )

    def count(self, name: str, value: int = 1) -> None:
        """Add one allowlisted workload count without per-row logging."""
        if name in PERFORMANCE_COUNTERS:
            self._counters[name] = min(
                2**53 - 1, self._counters.get(name, 0) + max(0, int(value))
            )

    def metrics(self, values: Mapping[str, int | float]) -> None:
        """Merge known numeric database and workload observations."""
        for name, value in values.items():
            if name in PERFORMANCE_COUNTERS:
                self.count(name, int(value))
            elif name in PERFORMANCE_METRICS:
                self._metrics[name] = self._metrics.get(name, 0) + _number(value)

    def observe_workers(self, value: int) -> None:
        """Record peak in-flight workers, independently of configured capacity."""
        self._observed_workers = max(self._observed_workers, max(0, min(4, value)))

    def worker(self) -> OperationPerformance:
        """Create independent state for an explicitly bound child thread."""
        return OperationPerformance(
            operation_id=self.operation_id,
            operation=self.operation,
            configured_workers=self.configured_workers,
        )

    def snapshot(self) -> PerformanceSnapshot:
        """Return immutable observed costs without customer or target context."""
        return PerformanceSnapshot(
            tuple(self._phases[name] for name in sorted(self._phases)),
            tuple(sorted(self._counters.items())),
            tuple(sorted(self._metrics.items())),
            self._peak_rss,
            self._observed_workers,
        )

    def merge(self, snapshot: PerformanceSnapshot) -> None:
        """Merge worker results on the coordinator, preserving bounded names."""
        for phase in snapshot.phases:
            self.record_phase(
                phase.name, phase.wall_ms, cpu_ms=phase.thread_cpu_ms, calls=phase.calls
            )
        self.metrics(dict(snapshot.counters))
        self.metrics(dict(snapshot.metrics))
        self.observe_workers(snapshot.observed_workers)
        if snapshot.peak_process_rss_bytes is not None:
            self._peak_rss = max(self._peak_rss or 0, snapshot.peak_process_rss_bytes)

    def finish(self, *, outcome: str, failure_code: str | None = None) -> None:
        """Emit one terminal summary; diagnostics never replace job results."""
        if self._finished:
            return
        self._finished = True
        self._sample_memory()
        phases = {
            name: {
                "wall_ms": phase.wall_ms,
                "thread_cpu_ms": phase.thread_cpu_ms,
                "calls": phase.calls,
            }
            for name, phase in self._phases.items()
        }
        payload: dict[str, object] = {
            "event": "performance_operation_completed",
            "outcome": outcome
            if outcome in {"succeeded", "failed", "interrupted"}
            else "failed",
            "wall_ms": _number((perf_counter() - self._started) * 1000),
            "coordinator_thread_cpu_ms": _number(
                (thread_time() - self._cpu_started) * 1000
            ),
            "queue_wait_ms": self.queue_wait_ms,
            "phases": phases,
            "counters": dict(self._counters),
            "metrics": dict(self._metrics),
            "peak_process_rss_bytes": self._peak_rss,
            "configured_workers": self.configured_workers,
            "observed_workers": self._observed_workers,
            "capture_failure_count": self._capture_failures,
            "coverage": {
                "phases": sorted(self._phases),
                "counters": sorted(self._counters),
                "metrics": sorted(self._metrics),
            },
        }
        if failure_code and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", failure_code):
            payload["failure_code"] = failure_code
        self._emit(payload)

    def _sample_memory(self) -> None:
        try:
            observed = psutil.Process().memory_info().rss
            self._peak_rss = max(self._peak_rss or 0, observed)
        except Exception:  # noqa: BLE001 - unsupported resource probes are optional
            self._capture_failures += 1

    def _emit(self, payload: dict[str, object]) -> None:
        if self._sink is not None:
            try:
                self._sink(
                    {
                        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
                        "operation_id": self.operation_id,
                        "operation": self.operation,
                        **payload,
                    }
                )
            except Exception:  # noqa: BLE001 - telemetry must not change job truth
                self._capture_failures += 1


_CURRENT: ContextVar[OperationPerformance | None] = ContextVar(
    "impodo_performance", default=None
)


@contextmanager
def bind_performance(collector: OperationPerformance | None) -> Iterator[None]:
    """Bind one collector to its current execution context, restoring nesting."""
    token = _CURRENT.set(collector)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_performance() -> OperationPerformance | None:
    """Return the collector that the current coordinator explicitly bound."""
    return _CURRENT.get()


@contextmanager
def performance_phase(name: str) -> Iterator[None]:
    """Measure known phases only when operation diagnostics are active."""
    collector = _CURRENT.get()
    if collector is None:
        yield
    else:
        with collector.phase(name):
            yield


def performance_count(name: str, value: int = 1) -> None:
    """Count known work without retaining record values or allocating events."""
    if (collector := _CURRENT.get()) is not None:
        collector.count(name, value)


def performance_observe_workers(value: int) -> None:
    """Record coordinator-observed concurrency without sharing worker state."""
    if (collector := _CURRENT.get()) is not None:
        collector.observe_workers(value)


def performance_merge(snapshot: PerformanceSnapshot | None) -> None:
    """Merge explicitly returned child costs into the current coordinator."""
    if snapshot is not None and (collector := _CURRENT.get()) is not None:
        collector.merge(snapshot)


def _number(value: float) -> float:
    numeric = float(value)
    return round(min(max(0, numeric), 86_400_000), 3) if math.isfinite(numeric) else 0
