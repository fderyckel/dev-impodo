"""Request-scoped, value-free DuckDB timing aggregation.

The collector deliberately stores only durations and operation counts. It has
no API for database paths, workspace identifiers, SQL, parameters, or row
values, so web diagnostics cannot accidentally retain business data.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(slots=True)
class DuckDbRequestTimings:
    """Aggregate bounded DuckDB infrastructure costs for one request."""

    connect_ms: float = 0.0
    schema_ms: float = 0.0
    lock_wait_ms: float = 0.0
    connection_count: int = 0
    schema_check_count: int = 0
    lock_retry_count: int = 0

    def record_connection(
        self,
        *,
        duration_ms: float,
        lock_wait_ms: float,
        lock_retry_count: int,
    ) -> None:
        self.connect_ms += max(0.0, float(duration_ms))
        self.lock_wait_ms += max(0.0, float(lock_wait_ms))
        self.connection_count += 1
        self.lock_retry_count += max(0, int(lock_retry_count))

    def record_schema_check(self, *, duration_ms: float) -> None:
        self.schema_ms += max(0.0, float(duration_ms))
        self.schema_check_count += 1

    def server_timings_ms(self) -> dict[str, float]:
        timings: dict[str, float] = {}
        if self.connect_ms > 0:
            timings["db_connect"] = self.connect_ms
        if self.schema_ms > 0:
            timings["db_schema"] = self.schema_ms
        if self.lock_wait_ms > 0:
            timings["db_lock_wait"] = self.lock_wait_ms
        return timings


_CURRENT_DUCKDB_TIMINGS: ContextVar[DuckDbRequestTimings | None] = ContextVar(
    "impodo_duckdb_request_timings",
    default=None,
)


@contextmanager
def collect_duckdb_request_timings() -> Iterator[DuckDbRequestTimings]:
    """Install one collector for the current async/thread execution context."""

    collector = DuckDbRequestTimings()
    token = _CURRENT_DUCKDB_TIMINGS.set(collector)
    try:
        yield collector
    finally:
        _CURRENT_DUCKDB_TIMINGS.reset(token)


def record_duckdb_connection_timing(
    *,
    duration_ms: float,
    lock_wait_ms: float,
    lock_retry_count: int,
) -> None:
    """Add one connection result when request diagnostics are active."""

    collector = _CURRENT_DUCKDB_TIMINGS.get()
    if collector is not None:
        collector.record_connection(
            duration_ms=duration_ms,
            lock_wait_ms=lock_wait_ms,
            lock_retry_count=lock_retry_count,
        )


def record_duckdb_schema_timing(*, duration_ms: float) -> None:
    """Add one exact-schema verification when request diagnostics are active."""

    collector = _CURRENT_DUCKDB_TIMINGS.get()
    if collector is not None:
        collector.record_schema_check(duration_ms=duration_ms)
