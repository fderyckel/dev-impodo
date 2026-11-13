"""Collect bounded costs at instrumented artifact I/O boundaries.

These observations describe application file operations, not physical disk
latency. Hash time includes the existing file read and digest calculation;
hash bytes are deliberately not also counted as ordinary read bytes. No path,
source value, log write, extra file read, or sampling thread is retained here.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Literal

FileIoOperation = Literal["read", "write", "hash"]
_MAX_COUNT = 9_223_372_036_854_775_807
_MAX_DURATION_MS = 86_400_000.0


@dataclass(slots=True)
class FileIoTransfer:
    """Count only bytes already transferred by the owning operation."""

    bytes_count: int = 0

    def add_bytes(self, count: int) -> None:
        """Add an observed transfer without retaining its content."""

        if type(count) is int and count >= 0:
            self.bytes_count = min(_MAX_COUNT, self.bytes_count + count)


@dataclass(slots=True)
class _OperationTotals:
    operations: int = 0
    bytes_count: int = 0
    duration_ms: float | None = 0.0


class FileIoPerformance:
    """Aggregate fixed-size, value-free facts for one bounded operation."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._totals: dict[FileIoOperation, _OperationTotals] = {
            "read": _OperationTotals(),
            "write": _OperationTotals(),
            "hash": _OperationTotals(),
        }
        self._failed_operations = 0
        self._capture_failures = 0

    def record(
        self,
        operation: FileIoOperation,
        *,
        bytes_count: int,
        duration_ms: float | None,
        failed: bool,
    ) -> None:
        """Retain one completed or interrupted instrumented operation."""

        with self._lock:
            total = self._totals[operation]
            total.operations = min(_MAX_COUNT, total.operations + 1)
            total.bytes_count = min(_MAX_COUNT, total.bytes_count + max(0, bytes_count))
            if duration_ms is None or not math.isfinite(duration_ms) or duration_ms < 0:
                total.duration_ms = None
            elif total.duration_ms is not None:
                total.duration_ms = min(
                    _MAX_DURATION_MS, total.duration_ms + duration_ms
                )
            if failed:
                self._failed_operations = min(_MAX_COUNT, self._failed_operations + 1)

    def capture_failed(self) -> None:
        """Mark missing optional evidence without changing the work outcome."""

        with self._lock:
            self._capture_failures = min(_MAX_COUNT, self._capture_failures + 1)

    def snapshot(self) -> dict[str, int | float | str]:
        """Return only observed totals with their explicit coverage and overlap."""

        with self._lock:
            result: dict[str, int | float | str] = {
                "scope": "instrumented_artifact_boundaries",
                "hash_scope": "read_and_hash_inclusive",
                "status": "observed"
                if any(total.operations for total in self._totals.values())
                else "no_observations",
                "capture_failure_count": self._capture_failures,
            }
            observed = False
            for operation, total in self._totals.items():
                if not total.operations:
                    continue
                observed = True
                result[f"{operation}_operations"] = total.operations
                result[f"{operation}_bytes"] = total.bytes_count
                if total.duration_ms is not None:
                    result[f"{operation}_ms"] = round(total.duration_ms, 3)
            if observed:
                result["failed_operations"] = self._failed_operations
            return result


_CURRENT: ContextVar[FileIoPerformance | None] = ContextVar(
    "impodo_file_io_performance", default=None
)


@contextmanager
def collect_file_io() -> Iterator[FileIoPerformance]:
    """Bind one collector; nested helpers reuse it instead of double-counting."""

    current = _CURRENT.get()
    if current is not None:
        yield current
        return
    collector = FileIoPerformance()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


@contextmanager
def measure_file_io(operation: FileIoOperation) -> Iterator[FileIoTransfer | None]:
    """Observe existing work while isolating clock and recorder failures."""

    collector = _CURRENT.get()
    if collector is None:
        yield None
        return
    transfer = FileIoTransfer()
    started: float | None = None
    try:
        started = perf_counter()
    except Exception:  # noqa: BLE001 - optional timing cannot prevent file work
        _capture_failed(collector)
    failed = True
    try:
        yield transfer
        failed = False
    finally:
        duration_ms: float | None = None
        if started is not None:
            try:
                duration_ms = (perf_counter() - started) * 1000
            except Exception:  # noqa: BLE001 - retain bytes even if the clock fails
                _capture_failed(collector)
        try:
            collector.record(
                operation,
                bytes_count=transfer.bytes_count,
                duration_ms=duration_ms,
                failed=failed,
            )
        except Exception:  # noqa: BLE001 - evidence must preserve the original exception
            _capture_failed(collector)


def _capture_failed(collector: FileIoPerformance) -> None:
    """Keep a failing optional observer from replacing a file operation."""

    try:
        collector.capture_failed()
    except Exception:  # noqa: BLE001 - a failed diagnostic cannot diagnose itself
        return
