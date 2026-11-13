"""Sanitize value-free performance records at capture and bundle export.

Only known phases, counters and numeric environment observations survive. The
summary remains useful when rotation retained a phase but no terminal event.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from impodo.application.shared.performance import (
    PERFORMANCE_COUNTERS,
    PERFORMANCE_METRICS,
    PERFORMANCE_PHASES,
    PERFORMANCE_SCHEMA_VERSION,
)
from impodo.web.performance_attribution import (
    request_attribution,
    sanitize_file_io,
    sanitize_numeric_validation,
)
from impodo.web.preparation_performance_diagnostics import (
    PERFORMANCE_EVENT,
    sanitize_performance_event,
)
from impodo.web.runtime_diagnostics import sanitize_runtime_metadata

PERFORMANCE_EVENTS = frozenset(
    {
        "performance_operation_started",
        "performance_phase_started",
        "performance_phase_completed",
        "performance_operation_completed",
    }
)
MAX_PERFORMANCE_ATTEMPTS = 30


def sanitize_performance_record(
    value: Mapping[str, object],
) -> dict[str, object] | None:
    """Keep the same strict schema for original capture and downloaded logs."""
    event, operation = value.get("event"), value.get("operation")
    if not isinstance(event, str) or not isinstance(operation, str):
        return None
    if event not in PERFORMANCE_EVENTS or operation not in {
        "preflight",
        "preparation",
    }:
        return None
    try:
        operation_id = str(UUID(str(value.get("operation_id", ""))))
    except ValueError:
        return None
    result: dict[str, object] = {
        "event": value["event"],
        "operation": value["operation"],
        "operation_id": operation_id,
        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
    }
    build_id = value.get("application_build_id")
    if isinstance(build_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", build_id):
        result["application_build_id"] = build_id
    for name in (
        "wall_ms",
        "thread_cpu_ms",
        "coordinator_thread_cpu_ms",
        "queue_wait_ms",
    ):
        if name in value:
            result[name] = _numeric(value[name], 86_400_000)
    for name in (
        "calls",
        "configured_workers",
        "observed_workers",
        "capture_failure_count",
    ):
        if (number := _numeric(value.get(name), 1_000_000)) is not None:
            result[name] = int(number)
    for name in ("process_rss_bytes", "peak_process_rss_bytes"):
        if name in value:
            result[name] = _numeric(value[name], 2**53 - 1)
    phase = value.get("phase")
    if isinstance(phase, str) and phase in PERFORMANCE_PHASES:
        result["phase"] = phase
    outcome = value.get("outcome")
    if isinstance(outcome, str) and outcome in {"succeeded", "failed", "interrupted"}:
        result["outcome"] = outcome
    failure_code = value.get("failure_code")
    if isinstance(failure_code, str) and re.fullmatch(
        r"[A-Z][A-Z0-9_]{0,99}", failure_code
    ):
        result["failure_code"] = failure_code
    for field, allowed in (
        ("counters", PERFORMANCE_COUNTERS),
        ("metrics", PERFORMANCE_METRICS),
    ):
        source = value.get(field)
        if isinstance(source, dict):
            result[field] = {
                name: number
                for name in sorted(allowed)
                if (number := _numeric(source.get(name), 2**53 - 1)) is not None
            }
    phases = value.get("phases")
    if isinstance(phases, dict):
        result["phases"] = {
            name: _phase(phases[name])
            for name in sorted(PERFORMANCE_PHASES)
            if isinstance(phases.get(name), dict)
        }
    runtime = value.get("runtime")
    if isinstance(runtime, dict):
        result["runtime"] = sanitize_runtime_metadata(runtime)
    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = {
            field: sorted(name for name in allowed if name in coverage.get(field, []))
            for field, allowed in (
                ("phases", PERFORMANCE_PHASES),
                ("counters", PERFORMANCE_COUNTERS),
                ("metrics", PERFORMANCE_METRICS),
            )
            if isinstance(coverage.get(field), list)
        }
    if "file_io" in value:
        result["file_io"] = sanitize_file_io(value["file_io"])
    if "numeric_validation" in value:
        result["numeric_validation"] = sanitize_numeric_validation(value["numeric_validation"])
    return result


def performance_summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Summarize recent attempts and explicitly expose missing terminal facts."""
    attempts: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("event") == PERFORMANCE_EVENT:
            safe = _preparation_summary_record(record.get("performance"))
        elif (
            isinstance(record.get("event"), str)
            and record.get("event") in PERFORMANCE_EVENTS
        ):
            safe = sanitize_performance_record(record)
        else:
            continue
        if safe is None:
            continue
        operation_id = str(safe["operation_id"])
        previous = attempts.pop(operation_id, {})
        current = {**previous, **safe}
        previous_active = previous.get("active_phases")
        active = list(previous_active) if isinstance(previous_active, list) else []
        if safe["event"] == "performance_phase_started":
            if safe.get("phase") in PERFORMANCE_PHASES and len(active) < len(
                PERFORMANCE_PHASES
            ):
                active.append(safe["phase"])
        elif (
            safe["event"] == "performance_phase_completed"
            and safe.get("phase") in active
        ):
            active.remove(safe["phase"])
        elif safe["event"] == "performance_operation_completed":
            active.clear()
        current["active_phases"] = active
        current["phase_in_progress"] = active[-1] if active else None
        previous_phases = previous.get("phases")
        if safe["event"] == "performance_phase_completed" and isinstance(
            safe.get("phase"), str
        ):
            phases = dict(previous_phases) if isinstance(previous_phases, dict) else {}
            name = str(safe["phase"])
            previous_phase = phases.get(name)
            existing = previous_phase if isinstance(previous_phase, dict) else {}
            wall = (_numeric(existing.get("wall_ms"), 86_400_000) or 0) + (
                _numeric(safe.get("wall_ms"), 86_400_000) or 0
            )
            cpu = safe.get("thread_cpu_ms")
            previous_cpu = existing.get("thread_cpu_ms")
            phases[name] = {
                "wall_ms": wall,
                "thread_cpu_ms": (
                    float(cpu) + (_numeric(previous_cpu, 86_400_000) or 0)
                    if isinstance(cpu, (int, float))
                    and (not existing or previous_cpu is not None)
                    else None
                ),
                "calls": int(_numeric(existing.get("calls"), 1_000_000) or 0)
                + int(_numeric(safe.get("calls"), 1_000_000) or 1),
            }
            current["phases"] = phases
        current["last_recorded_at"] = record.get("recorded_at")
        current["terminal_observed"] = safe[
            "event"
        ] == "performance_operation_completed" or previous.get(
            "terminal_observed", False
        )
        phases = current.get("phases")
        if isinstance(phases, dict):
            current["slowest_phases"] = sorted(
                (
                    {"phase": name, "wall_ms": values.get("wall_ms")}
                    for name, values in phases.items()
                    if isinstance(values, dict)
                ),
                key=lambda item: float(item["wall_ms"] or 0),
                reverse=True,
            )[:5]
        attempts[operation_id] = current
        if len(attempts) > MAX_PERFORMANCE_ATTEMPTS:
            attempts.pop(next(iter(attempts)))
    return {
        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
        "attempts": list(attempts.values()),
        "request_observations": request_attribution(records),
        "request_observation_limit": 100,
        "max_attempts": MAX_PERFORMANCE_ATTEMPTS,
        "coverage": "retained logs only; missing phases and terminal facts are unavailable",
        "phase_wall_time": "overlapping phase totals are not additive",
        "memory_measurement": "sampled process-wide RSS; peak between samples may be higher",
        "cpu_measurement": "preflight uses phase/coordinator thread CPU; preparation resource counters use parent/worker process CPU",
        "browser_timing": "client network, browser rendering and click-to-visible latency are unavailable",
        "file_io_measurement": "instrumented artifact boundaries; overlapping reads and hashes are not physical disk throughput",
        "antivirus_attribution": "unavailable; file operation delays alone do not identify a security product",
    }


def _preparation_summary_record(value: object) -> dict[str, object] | None:
    """Normalize the preparation supervisor's own closed schema for summaries."""
    event = sanitize_performance_event(value)
    if event is None:
        return None
    kinds = {
        "attempt_started": "performance_operation_started",
        "phase_completed": "performance_phase_completed",
        "attempt_finished": "performance_operation_completed",
    }
    result: dict[str, object] = {
        "event": kinds[str(event["event_kind"])],
        "operation": "preparation",
        "operation_id": event["attempt_id"],
        "wall_ms": event.get("duration_ms"),
    }
    for name in (
        "environment",
        "configuration",
        "workload",
        "resources",
        "database",
        "file_io",
        "numeric_validation",
        "measurement_complete",
        "failure_reason",
    ):
        if name in event:
            result[name] = event[name]
    if event.get("event_kind") == "attempt_finished":
        result["outcome"] = event.get("outcome")
    phase = event.get("phase")
    if isinstance(phase, str):
        result.update({"phase": phase, "calls": 1, "thread_cpu_ms": None})
        if phase == "queue_wait":
            result["queue_wait_ms"] = event.get("duration_ms")
    environment = event.get("environment")
    if isinstance(environment, dict) and "build_id" in environment:
        result["application_build_id"] = environment["build_id"]
    return result


def _phase(value: dict[str, object]) -> dict[str, object]:
    return {
        "wall_ms": _numeric(value.get("wall_ms"), 86_400_000),
        "thread_cpu_ms": _numeric(value.get("thread_cpu_ms"), 86_400_000),
        "calls": _numeric(value.get("calls"), 1_000_000),
    }


def _numeric(value: object, maximum: int) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return (
        round(float(value), 3)
        if 0 <= value <= maximum and math.isfinite(value)
        else None
    )
