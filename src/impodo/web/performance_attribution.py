"""Allowlist operational costs without exporting paths or customer values.

File observations describe instrumented application boundaries, not physical
disk throughput. Recorder costs are captured in memory and cannot identify an
antivirus product as the cause of an observed delay.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast
from uuid import UUID

_MAX_COUNT = 2**53 - 1
_MAX_MS = 86_400_000
FILE_IO_COUNTS = frozenset(
    {"read_operations", "read_bytes", "write_operations", "write_bytes",
     "hash_operations", "hash_bytes", "failed_operations"}
)
FILE_IO_TIMINGS = frozenset({"read_ms", "write_ms", "hash_ms"})
STARTUP_COUNTS = frozenset(
    {"worker_count", "build_hash_files", "build_hash_bytes", "start_failure_count",
     "readiness_count", "result_count"}
)
STARTUP_TIMINGS = frozenset(
    {"start_call_ms", "first_message_wait_ms", "result_wait_ms", "build_hash_ms"}
)
NUMERIC_COUNTS = frozenset(
    {"cells_examined", "findings_generated", "value_page_count",
     "failure_batch_count", "max_value_page_rows", "max_value_page_bytes",
     "max_failure_batch_rows", "max_failure_batch_bytes"}
)
LOGGING_COUNTS = frozenset(
    {"diagnostic_write_count", "diagnostic_bytes_written",
     "diagnostic_lock_timeout_count", "diagnostic_rotation_count",
     "diagnostic_capture_failure_count", "request_completed_count"}
)
LOGGING_TIMINGS = frozenset(
    {"diagnostic_encode_ms", "diagnostic_thread_lock_wait_ms",
     "diagnostic_file_lock_wait_ms", "diagnostic_emit_ms", "diagnostic_open_ms",
     "diagnostic_write_flush_ms", "diagnostic_close_ms", "diagnostic_rotation_ms",
     "request_reported_ms", "request_lifecycle_ms"}
)


def sanitize_file_io(value: object) -> dict[str, object]:
    """Keep measured artifact-boundary counts and inclusive durations only."""

    raw = _mapping(value)
    if raw.get("scope") != "instrumented_artifact_boundaries":
        return {}
    return {
        "scope": "instrumented_artifact_boundaries",
        **_numbers(raw, FILE_IO_COUNTS, FILE_IO_TIMINGS),
    }


def sanitize_worker_startup(value: object) -> dict[str, object]:
    """Retain source-worker parent intervals and observed child build facts."""

    raw = _mapping(value)
    if raw.get("scope") != "source_worker_parent_intervals":
        return {}
    operations = _mapping(raw.get("operations"))
    return {
        "scope": "source_worker_parent_intervals",
        "operations": {
            name: _numbers(_mapping(operations[name]), STARTUP_COUNTS, STARTUP_TIMINGS)
            for name in ("validation", "inspection")
            if isinstance(operations.get(name), Mapping)
        },
    }


def sanitize_numeric_validation(value: object) -> dict[str, object]:
    """Keep bounded numeric-validation workload facts without inspected values."""

    raw = _mapping(value)
    if not raw:
        return {}
    return _numbers(raw, NUMERIC_COUNTS, frozenset({"elapsed_ms"}))


def sanitize_logging_costs(value: object) -> dict[str, object]:
    """Resanitize the exporting recorder's bounded in-memory measurements."""

    return _logging_costs(value, include_children=True)


def _logging_costs(value: object, *, include_children: bool) -> dict[str, object]:
    """Keep one recorder tree level and explicit non-additive correlations."""

    raw = _mapping(value)
    if raw.get("schema_version") != 1 or raw.get("scope") != "process_recorder":
        return {"status": "unavailable"}
    metrics = _numbers(_mapping(raw.get("metrics")), LOGGING_COUNTS, LOGGING_TIMINGS)
    result: dict[str, object] = {
        "schema_version": 1,
        "scope": "process_recorder",
        "status": "available" if metrics else "unavailable",
        "metrics": metrics,
    }
    for name in ("capture_complete", "component_timings_may_overlap", "correlations_are_non_additive"):
        if type(raw.get(name)) is bool:
            result[name] = raw[name]
    result["child_process_coverage"] = "unavailable"
    result["timing_scope"] = "inclusive"
    result["correlation_limit"] = 64
    result.update(_numbers(raw, frozenset({"dropped_correlation_count"}), frozenset()))
    correlations: list[dict[str, object]] = []
    items = raw.get("correlations")
    for item in items[:64] if isinstance(items, list) else ():
        item_raw = _mapping(item)
        if item_raw.get("kind") not in {"request", "operation"}:
            continue
        try:
            identity = str(UUID(str(item_raw.get("id", ""))))
        except (ValueError, AttributeError):
            continue
        safe: dict[str, object] = {
            "kind": item_raw["kind"],
            "id": identity,
            "metrics": _numbers(_mapping(item_raw.get("metrics")), LOGGING_COUNTS, LOGGING_TIMINGS)
        }
        correlations.append(safe)
    if correlations:
        result["correlations"] = correlations
    if include_children:
        children = _mapping(raw.get("load_recorders"))
        result["load_recorders"] = {
            name: _logging_costs(children[name], include_children=False)
            for name in ("events", "summary")
            if name in children
        }
    return result


def _numbers(
    raw: Mapping[str, object], counts: frozenset[str], timings: frozenset[str]
) -> dict[str, object]:
    """Reject booleans, invalid magnitudes and non-finite observations."""

    result: dict[str, object] = {}
    for name in sorted(counts):
        item = raw.get(name)
        if type(item) is int and 0 <= item <= _MAX_COUNT:
            result[name] = item
    for name in sorted(timings):
        item = raw.get(name)
        if (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and 0 <= item <= _MAX_MS
            and math.isfinite(item)
        ):
            result[name] = round(float(item), 3)
    return result


def _mapping(value: object) -> Mapping[str, object]:
    """Narrow untrusted optional sections without supplying invented values."""

    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}
