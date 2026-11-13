"""Aggregate optional source-worker startup costs without retaining input data.

Parent intervals use the parent's monotonic clock. Child build costs are
durations reported by that child; timestamps are never subtracted across
processes. The first-message wait includes IPC and parent scheduling.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Literal

WorkerKind = Literal["validation", "inspection"]
_KINDS = frozenset({"validation", "inspection"})
_COUNTERS = frozenset(
    {
        "worker_count", "start_failure_count", "readiness_count", "result_count",
        "build_hash_files", "build_hash_bytes",
    }
)
_DURATIONS = frozenset(
    {"start_call_ms", "first_message_wait_ms", "result_wait_ms", "build_hash_ms"}
)
_MAX_COUNTER = 9_223_372_036_854_775_807
_MAX_DURATION = 86_400_000.0


class WorkerStartupObservation:
    """Retain at most two fixed-size, value-free operation summaries."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._operations: dict[str, dict[str, int | float]] = {}

    def record(self, kind: WorkerKind, values: Mapping[str, int | float]) -> None:
        """Merge only finite, nonnegative allowlisted measurements."""

        if kind not in _KINDS:
            return
        with self._lock:
            current = self._operations.setdefault(kind, {})
            for name, value in values.items():
                if name in _COUNTERS and type(value) is int and value >= 0:
                    current[name] = min(
                        _MAX_COUNTER, int(current.get(name, 0)) + value
                    )
                elif (
                    name in _DURATIONS
                    and type(value) in {int, float}
                    and math.isfinite(value)
                    and value >= 0
                ):
                    current[name] = min(
                        _MAX_DURATION, float(current.get(name, 0)) + value
                    )

    def snapshot(self) -> dict[str, object]:
        """Copy measured fields only; absence is not a zero-cost observation."""

        with self._lock:
            return {
                "scope": "source_worker_parent_intervals",
                "operations": {
                    name: dict(values) for name, values in self._operations.items()
                },
            }


_CURRENT: ContextVar[WorkerStartupObservation | None] = ContextVar(
    "impodo_worker_startup", default=None
)


@contextmanager
def collect_worker_startup() -> Iterator[WorkerStartupObservation]:
    """Bind one bounded collection to the current operation context."""

    observation = WorkerStartupObservation()
    token = _CURRENT.set(observation)
    try:
        yield observation
    finally:
        _CURRENT.reset(token)


def record_worker_startup(kind: WorkerKind, **values: int | float) -> None:
    """Keep optional diagnostic failures outside the worker's outcome."""

    try:
        observation = _CURRENT.get()
        if observation is not None:
            observation.record(kind, values)
    except Exception:  # noqa: BLE001 - optional observation must not alter source processing
        return
