"""Observe bounded preparation resource use without influencing job outcomes.

The supervisor samples RSS at most once per second. These observations are
sampled resident memory, not an operating-system peak or a process memory cap.
Combined RSS sums the two processes and can count their shared pages twice.
Worker facts pass through the same allowlist used by diagnostic ZIP exports.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Callable, Mapping
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from threading import RLock
from time import perf_counter

import psutil

from impodo.web.preparation_performance_diagnostics import sanitize_performance_event

SAMPLE_INTERVAL_SECONDS = 1.0
MAX_RETAINED_PHASES = 128
_SECTIONS = (
    "environment",
    "configuration",
    "workload",
    "resources",
    "database",
)
PerformanceRecorder = Callable[[Mapping[str, object]], None]


def execution_environment(build_id: str) -> dict[str, object]:
    """Capture execution-time platform facts without importing native engines."""

    try:
        return _execution_environment(build_id)
    except Exception:  # noqa: BLE001 - optional platform observation cannot fail preparation
        return {"build_id": build_id}


def _execution_environment(build_id: str) -> dict[str, object]:
    result: dict[str, object] = {
        "operating_system": platform.system(),
        "os_version": platform.release(),
        "architecture": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": ".".join(str(value) for value in sys.version_info[:3]),
        "build_id": build_id,
    }
    for package in ("polars", "duckdb"):
        try:
            result[f"{package}_version"] = version(package)
        except PackageNotFoundError:
            pass
    logical_cpus = os.cpu_count()
    if logical_cpus is not None:
        result["logical_cpu_count"] = logical_cpus
    try:
        memory = psutil.virtual_memory()
        result["total_memory_bytes"] = int(memory.total)
        result["available_memory_bytes"] = int(memory.available)
    except (psutil.Error, OSError):
        pass
    try:
        affinity = psutil.Process().cpu_affinity()
        if affinity:
            result["available_cpu_count"] = len(affinity)
    except (AttributeError, NotImplementedError, psutil.Error, OSError):
        pass
    return result


class PreparationPerformanceObservation:
    """Keep bounded operational evidence for one supervisor-owned attempt.

    Call ``sample`` from the existing supervisor loop. No sampling thread or
    database connection is created. The recorder is optional; benchmark callers
    can inspect the same sanitized facts through ``evidence``.
    """

    def __init__(
        self,
        attempt_id: str,
        total_rows: int,
        queued_at: float,
        recorder: PerformanceRecorder | None = None,
    ) -> None:
        self._lock = RLock()
        self._attempt_id = attempt_id
        self._queued_at = queued_at
        self._recorder = recorder
        self._started_at: float | None = None
        self._finished = False
        self._sequence = 0
        self._worker_pid: int | None = None
        self._last_sample_at: float | None = None
        self._first_sample_at: float | None = None
        self._parent_cpu_start: float | None = None
        self._sampling_failures = 0
        self._capture_failures = 0
        self._sample_count = 0
        self._last_event: dict[str, object] = {}
        self._phases: list[dict[str, object]] = []
        self._sections: dict[str, dict[str, object]] = {
            "configuration": {
                "worker_processes": 1,
                "duckdb_configuration": "UNCHANGED",
            },
            "workload": {"row_count": total_rows},
            "resources": {"sample_interval_ms": SAMPLE_INTERVAL_SECONDS * 1000},
        }

    def start(self, worker_pid: int | None, build_id: str) -> None:
        """Record launch and queue wait; isolate all optional collection errors."""

        try:
            with self._lock:
                if self._started_at is not None or self._finished:
                    return
                self._started_at = perf_counter()
                self._worker_pid = worker_pid
                self._merge({"environment": execution_environment(build_id)})
                self._emit("attempt_started", outcome="started")
                self.phase(
                    "queue_wait",
                    max(0.0, self._started_at - self._queued_at) * 1000,
                    timing_scope="exclusive",
                )
                self.sample()
        except Exception:  # noqa: BLE001 - optional resource diagnostics cannot break a job
            self._capture_failures += 1

    def attach_worker_pid(self, worker_pid: int) -> None:
        """Attach a successfully spawned process and take its first RSS sample."""

        try:
            with self._lock:
                if self._finished:
                    return
                self._worker_pid = worker_pid
                self.sample()
        except Exception:  # noqa: BLE001 - optional process observation cannot fail launch
            self._capture_failures += 1

    def sample(self) -> None:
        """Sample the parent and worker at most once per second while active."""

        with self._lock:
            if self._finished or self._worker_pid is None:
                return
            observed_at = perf_counter()
            if self._last_sample_at is not None and (
                observed_at - self._last_sample_at < SAMPLE_INTERVAL_SECONDS
            ):
                return
            self._last_sample_at = observed_at
            try:
                parent = psutil.Process()
                worker = psutil.Process(self._worker_pid)
                parent_rss = int(parent.memory_info().rss)
                worker_rss = int(worker.memory_info().rss)
                parent_cpu = parent.cpu_times()
                worker_cpu = worker.cpu_times()
                parent_seconds = float(parent_cpu.user + parent_cpu.system)
                if self._parent_cpu_start is None:
                    self._parent_cpu_start = parent_seconds
                resources = self._sections["resources"]
                for name, value in (
                    ("sampled_peak_parent_rss_bytes", parent_rss),
                    ("sampled_peak_worker_rss_bytes", worker_rss),
                    ("sampled_peak_combined_rss_bytes", parent_rss + worker_rss),
                ):
                    previous = resources.get(name)
                    resources[name] = max(
                        previous if isinstance(previous, int) else 0, value
                    )
                resources["parent_cpu_seconds"] = max(
                    0.0, parent_seconds - self._parent_cpu_start
                )
                resources["cpu_seconds"] = float(worker_cpu.user + worker_cpu.system)
                self._sample_count += 1
                if self._first_sample_at is None:
                    self._first_sample_at = observed_at
                resources["sampling_duration_ms"] = (
                    max(
                        0.0,
                        observed_at - self._first_sample_at,
                    )
                    * 1000
                )
            except Exception:  # noqa: BLE001 - disappearing or inaccessible processes are optional evidence
                self._sampling_failures += 1

    def accept_worker_event(self, payload: Mapping[str, object]) -> None:
        """Merge only allowlisted worker measurements into this attempt."""

        try:
            with self._lock:
                if self._finished:
                    return
                self._merge(payload)
        except Exception:  # noqa: BLE001 - malformed optional evidence cannot fail preparation
            self._capture_failures += 1

    def worker_started(self) -> None:
        """Record process launch through the first child entry-point event."""

        try:
            with self._lock:
                if self._started_at is None or self._finished:
                    return
                self.phase(
                    "worker_startup",
                    max(0.0, perf_counter() - self._started_at) * 1000,
                    timing_scope="exclusive",
                )
        except Exception:  # noqa: BLE001 - optional startup timing cannot fail a job
            self._capture_failures += 1

    def phase(
        self,
        stage: str,
        duration_ms: float,
        outcome: str = "completed",
        *,
        timing_scope: str = "inclusive",
        total_rows: int | None = None,
    ) -> None:
        """Publish one bounded phase, preserving overlap information."""

        try:
            with self._lock:
                if self._finished:
                    return
                if total_rows is not None:
                    self._sections.setdefault("workload", {})["row_count"] = total_rows
                self._emit(
                    "phase_completed",
                    phase=stage,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    timing_scope=timing_scope,
                )
                if len(self._phases) < MAX_RETAINED_PHASES:
                    self._phases.append(
                        {
                            key: deepcopy(value)
                            for key, value in self._last_event.items()
                            if key
                            in {
                                "phase",
                                "duration_ms",
                                "outcome",
                                "timing_scope",
                                "sequence",
                            }
                        }
                    )
                else:
                    self._capture_failures += 1
        except Exception:  # noqa: BLE001 - optional phase evidence cannot change its outcome
            self._capture_failures += 1

    def finish(
        self,
        outcome: str,
        complete: bool,
        *,
        failure_reason: str | None = None,
    ) -> None:
        """Close once, including explicit incomplete/crashed measurement states."""

        try:
            with self._lock:
                if self._finished:
                    return
                fields: dict[str, object] = {
                    "outcome": outcome,
                    "measurement_complete": complete and self._capture_failures == 0,
                    "duration_ms": max(0.0, perf_counter() - self._queued_at) * 1000,
                }
                if failure_reason is not None:
                    fields["failure_reason"] = failure_reason
                self._emit("attempt_finished", **fields)
                self._finished = True
        except Exception:  # noqa: BLE001 - terminal telemetry cannot replace the saved job result
            self._capture_failures += 1

    def evidence(self) -> dict[str, object]:
        """Return an isolated sanitized snapshot for controlled comparisons."""

        with self._lock:
            result = deepcopy(self._last_event)
            for name, values in self._sections.items():
                result[name] = deepcopy(values)
            result["phases"] = deepcopy(self._phases)
            return result

    def _merge(self, payload: Mapping[str, object]) -> None:
        safe = sanitize_performance_event(
            {
                **payload,
                "schema_version": 1,
                "attempt_id": self._attempt_id,
                "operation": "preparation",
                "event_kind": "attempt_started",
            }
        )
        if safe is None:
            self._capture_failures += 1
            return
        for name in _SECTIONS:
            section = safe.get(name)
            if isinstance(section, dict):
                self._sections.setdefault(name, {}).update(section)

    def _emit(self, event_kind: str, **fields: object) -> None:
        resources = self._sections["resources"]
        resources.update(
            {
                "sample_count": self._sample_count,
                "sampling_failures": self._sampling_failures + self._capture_failures,
                "sampling_complete": self._sample_count > 0
                and self._sampling_failures == 0,
            }
        )
        payload = sanitize_performance_event(
            {
                **self._sections,
                **fields,
                "schema_version": 1,
                "attempt_id": self._attempt_id,
                "operation": "preparation",
                "event_kind": event_kind,
                "sequence": self._sequence,
            }
        )
        if payload is None:
            self._capture_failures += 1
            return
        self._last_event = payload
        self._sequence += 1
        if self._recorder is not None:
            try:
                self._recorder(payload)
            except Exception:  # noqa: BLE001 - diagnostic write failure cannot affect preparation
                self._capture_failures += 1
