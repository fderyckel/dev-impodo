"""Observe local load costs through the existing DuckDB timing collector.

The observer retains no database owner and performs no query. Nested stages
share the collector and report inclusive deltas, including zero measured work.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from time import thread_time

import psutil

from impodo.adapters.duckdb.request_timing import (
    collect_duckdb_request_timings,
    current_duckdb_request_timings,
)
from impodo.application.workspace.execution.load_observation import (
    LOAD_LOCAL_PHASES,
)

LOAD_COST_COUNTS = frozenset(
    {
        "calls",
        "failed_calls",
        "database_connection_count",
        "database_schema_check_count",
        "database_lock_retry_count",
        "capture_failure_count",
    }
)
LOAD_COST_TIMINGS = frozenset(
    {"wall_ms", "thread_cpu_ms", "db_connect_ms", "db_schema_ms", "db_lock_wait_ms"}
)


@contextmanager
def collect_load_stage_metrics(
    sample_memory: bool = True,
) -> Iterator[dict[str, object]]:
    """Measure one phase without holding a database or sampling RSS per row."""
    existing = current_duckdb_request_timings()
    scope = (
        nullcontext(existing)
        if existing is not None
        else collect_duckdb_request_timings()
    )
    result: dict[str, object] = {}
    with scope as timings:
        before = (
            timings.connection_count,
            timings.schema_check_count,
            timings.lock_retry_count,
            timings.connect_ms,
            timings.schema_ms,
            timings.lock_wait_ms,
        )
        started_cpu = thread_time()
        try:
            yield result
        finally:
            result.update(
                {
                    "database_connection_count": max(
                        0, timings.connection_count - before[0]
                    ),
                    "database_schema_check_count": max(
                        0, timings.schema_check_count - before[1]
                    ),
                    "database_lock_retry_count": max(
                        0, timings.lock_retry_count - before[2]
                    ),
                    "db_connect_ms": max(0.0, timings.connect_ms - before[3]),
                    "db_schema_ms": max(0.0, timings.schema_ms - before[4]),
                    "db_lock_wait_ms": max(0.0, timings.lock_wait_ms - before[5]),
                    "thread_cpu_ms": max(0.0, (thread_time() - started_cpu) * 1000),
                    "cpu_scope": "stage_context_thread_only"
                    if sample_memory
                    else "owning_local_thread",
                }
            )
            if sample_memory:
                try:
                    result["process_rss_bytes"] = psutil.Process().memory_info().rss
                except Exception:  # noqa: BLE001 - optional resource sampling cannot change load truth
                    result["capture_failure_count"] = 1


def sanitize_load_costs(value: object) -> dict[str, object]:
    """Keep closed numeric local-phase costs without arbitrary labels or paths."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for name in LOAD_COST_COUNTS:
        candidate = value.get(name)
        if type(candidate) is int and 0 <= candidate <= 2**53 - 1:
            result[name] = candidate
    for name in LOAD_COST_TIMINGS:
        candidate = value.get(name)
        if (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and 0 <= candidate <= 86_400_000
            and math.isfinite(candidate)
        ):
            result[name] = round(float(candidate), 3)
    memory = value.get("process_rss_bytes")
    if type(memory) is int and 0 <= memory <= 2**53 - 1:
        result["process_rss_bytes"] = memory
    scope = value.get("cpu_scope")
    result["cpu_scope"] = (
        scope
        if isinstance(scope, str)
        and scope
        in {
            "stage_context_thread_only",
            "owning_local_thread",
        }
        else "unavailable"
    )
    result["coverage"] = {
        "capture_complete": result.get("capture_failure_count", 0) == 0,
        "database": "available"
        if all(
            name in result
            for name in (
                "database_connection_count",
                "database_schema_check_count",
                "database_lock_retry_count",
                "db_connect_ms",
                "db_schema_ms",
                "db_lock_wait_ms",
            )
        )
        else "unavailable",
        "thread_cpu": "available" if "thread_cpu_ms" in result else "unavailable",
        "process_rss": "sampled" if "process_rss_bytes" in result else "unavailable",
    }
    return result


def sanitize_load_phase_metrics(value: object) -> dict[str, object]:
    """Keep only aggregate phase names owned by the load observer."""
    if not isinstance(value, Mapping):
        return {}
    return {
        name: sanitize_load_costs({**value[name], "cpu_scope": "owning_local_thread"})
        if isinstance(value[name], Mapping)
        else {}
        for name in sorted(LOAD_LOCAL_PHASES)
        if name in value
    }
