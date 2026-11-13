"""Summarize bounded preparation measurements from execution-time evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from .performance_attribution import request_attribution
from .preparation_performance_diagnostics import (
    PERFORMANCE_EVENT,
    sanitize_performance_event,
)

MAX_PERFORMANCE_EVENTS = 1_024
MAX_PERFORMANCE_ATTEMPTS = 20


def performance_bundle_members(
    records: Sequence[Mapping[str, object]],
    *,
    omitted_record_count: int = 0,
    capture_failure_count: int = 0,
) -> tuple[dict[str, str], dict[str, object]]:
    """Build JSON/text members without treating observations as benchmarks."""
    available: list[tuple[str, dict[str, object]]] = []
    rejected = 0
    for record in records:
        if record.get("event") != PERFORMANCE_EVENT:
            continue
        payload = sanitize_performance_event(record.get("performance"))
        if payload is None:
            rejected += 1
            continue
        available.append((str(record.get("recorded_at", "")), payload))
    grouped: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for recorded_at, payload in available[-MAX_PERFORMANCE_EVENTS:]:
        grouped.setdefault(str(payload["attempt_id"]), []).append(
            (recorded_at, payload)
        )
    selected = tuple(grouped.items())[-MAX_PERFORMANCE_ATTEMPTS:]
    attempts = [_attempt_summary(attempt_id, events) for attempt_id, events in selected]
    retained_count = sum(len(events) for _, events in selected)
    coverage: dict[str, object] = {
        "available_event_count": len(available),
        "retained_event_count": retained_count,
        "omitted_event_count": len(available) - retained_count,
        "omitted_attempt_count": max(0, len(grouped) - len(selected)),
        "rejected_event_count": rejected,
        "general_omitted_record_count": max(0, omitted_record_count),
        "recording_failure_count_current_session": max(0, capture_failure_count),
        "max_events": MAX_PERFORMANCE_EVENTS,
        "max_attempts": MAX_PERFORMANCE_ATTEMPTS,
        "history_before_retained_logs": "unavailable; rotated records may have expired",
        "status": "available" if attempts else "unavailable",
    }
    document: dict[str, object] = {
        "performance_schema_version": 1,
        "evidence_kind": "user_diagnostic_observations",
        "coverage": coverage,
        "attempts": attempts,
        "interpretation": {
            "runtime_context": "captured at execution time; missing fields are unavailable",
            "memory": "sampled resident memory; not an exact peak or a hard memory limit",
            "timings": "inclusive and nested phases overlap; do not add them together",
            "comparison": "a single observation cannot establish a speedup or a platform qualification",
        },
    }
    return {
        "performance.json": json.dumps(
            document, ensure_ascii=True, indent=2, sort_keys=True
        )
        + "\n",
        "performance-summary.txt": _text_summary(attempts, coverage, request_attribution(records)),
    }, coverage


def _attempt_summary(
    attempt_id: str, events: Sequence[tuple[str, dict[str, object]]]
) -> dict[str, object]:
    payloads = [payload for _, payload in events]
    started = [
        payload for payload in payloads if payload["event_kind"] == "attempt_started"
    ]
    finished = [
        payload for payload in payloads if payload["event_kind"] == "attempt_finished"
    ]
    terminal = finished[-1] if finished else {}
    sequences = [payload.get("sequence") for payload in payloads]
    has_sequence = all(type(sequence) is int for sequence in sequences)
    contiguous = has_sequence and sequences == list(range(len(sequences)))
    result: dict[str, object] = {
        "attempt_id": attempt_id,
        "operation": payloads[0]["operation"],
        "first_recorded_at": events[0][0],
        "last_recorded_at": events[-1][0],
        "event_count": len(events),
        "outcome": terminal.get("outcome", "unavailable"),
        "producer_measurement_complete": terminal.get(
            "measurement_complete", "unavailable"
        ),
        "coverage": {
            "start_present": bool(started),
            "terminal_present": bool(finished),
            "sequence_status": "complete"
            if contiguous
            else "partial"
            if has_sequence
            else "unavailable",
            "retained_history_complete": contiguous
            and len(started) == len(finished) == 1,
        },
        "phase_timings": [
            {
                key: payload[key]
                for key in (
                    "phase",
                    "duration_ms",
                    "timing_scope",
                    "outcome",
                    "sequence",
                )
                if key in payload
            }
            for payload in payloads
            if payload["event_kind"] == "phase_completed"
        ],
    }
    for key in ("duration_ms", "failure_reason"):
        if key in terminal:
            result[key] = terminal[key]
    for section in (
        "environment",
        "configuration",
        "workload",
        "resources",
        "database",
        "file_io",
        "numeric_validation",
    ):
        merged: dict[str, object] = {}
        for payload in payloads:
            raw = payload.get(section)
            if isinstance(raw, Mapping):
                merged.update(cast("Mapping[str, object]", raw))
        result[section] = merged
    result["section_coverage"] = {
        name: "available" if result[name] else "unavailable"
        for name in (
            "environment",
            "configuration",
            "workload",
            "resources",
            "database",
            "file_io",
            "numeric_validation",
        )
    }
    return result


def _text_summary(
    attempts: Sequence[dict[str, object]], coverage: Mapping[str, object],
    requests: Sequence[Mapping[str, object]] = (),
) -> str:
    lines = [
        "Impodo performance observations",
        "",
        "Execution-time facts from retained diagnostic logs.",
        "A single observation cannot establish a speedup or qualify Windows/macOS performance.",
        "Memory is sampled resident memory, not an exact peak or a hard memory limit.",
        "Inclusive and nested phase timings overlap; do not add them together.",
        "File observations measure instrumented operations, not physical disk throughput.",
        "Antivirus attribution is unavailable without separate authorized system evidence.",
        f"Retained attempts: {len(attempts)}; omitted events: {coverage['omitted_event_count']}.",
    ]
    if not attempts:
        lines.extend(
            (
                "",
                "Performance evidence unavailable. Older builds or expired logs may have no capture.",
            )
        )
    for attempt in attempts:
        environment = cast("Mapping[str, object]", attempt["environment"])
        configuration = cast("Mapping[str, object]", attempt["configuration"])
        resources = cast("Mapping[str, object]", attempt["resources"])
        file_io = cast("Mapping[str, object]", attempt["file_io"])
        numeric = cast("Mapping[str, object]", attempt["numeric_validation"])
        workload = cast("Mapping[str, object]", attempt["workload"])
        attempt_coverage = cast("Mapping[str, object]", attempt["coverage"])
        lines.extend(
            (
                "",
                f"Attempt: {attempt['attempt_id']} ({attempt['operation']})",
                f"Outcome: {attempt['outcome']}; retained history: {'complete' if attempt_coverage['retained_history_complete'] else 'partial or unavailable'}.",
                f"OS: {environment.get('operating_system', 'unavailable')} {environment.get('os_version', '')}; Python: {environment.get('python_version', 'unavailable')}.",
                f"Polars threads requested/effective: {configuration.get('polars_threads_requested', 'unavailable')}/{configuration.get('polars_threads_effective', 'unavailable')}; source: {configuration.get('source', 'unavailable')}.",
                f"Total elapsed ms: {attempt.get('duration_ms', 'unavailable')}; worker CPU seconds: {resources.get('cpu_seconds', 'unavailable')}.",
                f"Sampled peak worker RSS bytes: {resources.get('sampled_peak_worker_rss_bytes', 'unavailable')}; samples: {resources.get('sample_count', 'unavailable')}.",
            )
        )
        if file_io:
            lines.append(
                f"Artifact reads/hashes/writes: {file_io.get('read_operations', 'unavailable')}/"
                f"{file_io.get('hash_operations', 'unavailable')}/"
                f"{file_io.get('write_operations', 'unavailable')}; "
                "durations describe separate instrumented boundaries."
            )
        if numeric:
            lines.append(
                f"Numeric cells examined: {numeric.get('cells_examined', 'unavailable')}; "
                f"findings: {numeric.get('findings_generated', 'unavailable')}; "
                f"validation ms: {numeric.get('elapsed_ms', 'unavailable')}."
            )
        dataset_routes = workload.get("dataset_routes")
        if isinstance(dataset_routes, list):
            for route in dataset_routes[:8]:
                if isinstance(route, dict):
                    lines.append(
                        f"  Dataset {str(route.get('dataset_hash', 'unavailable'))[:19]}: "
                        f"{route.get('row_count', 'unavailable')} rows; "
                        f"predicted {route.get('predicted_route', 'UNOBSERVED')}; "
                        f"observed {route.get('actual_route', 'UNOBSERVED')}; "
                        f"adaptation {route.get('adaptation_route', 'UNOBSERVED')}; "
                        f"reasons {','.join(route.get('reason_codes', []))}; "
                        f"dataset ms {route.get('duration_ms', 'unavailable')}."
                    )
            lines.append("Dataset route snapshots replace earlier snapshots; repeated rows are not additive.")
        phases = cast("Sequence[Mapping[str, object]]", attempt["phase_timings"])
        for phase in sorted(
            phases,
            key=lambda item: float(cast("float", item.get("duration_ms", 0))),
            reverse=True,
        )[:8]:
            lines.append(
                f"  {phase['phase']}: {phase['duration_ms']} ms ({phase.get('timing_scope', 'scope unavailable')})."
            )
    if requests:
        lines.extend(("", "Recent request-owned file and worker observations (not additive to job phases):"))
        for request in requests[-8:]:
            file_io = cast("Mapping[str, object]", request.get("file_io", {}))
            startup = cast("Mapping[str, object]", request.get("worker_startup", {}))
            lines.append(
                f"  Request {request['request_id']}: reported ms {request.get('duration_ms', 'unavailable')}; "
                f"file read/hash/write ms {file_io.get('read_ms', 'unavailable')}/"
                f"{file_io.get('hash_ms', 'unavailable')}/{file_io.get('write_ms', 'unavailable')}."
            )
            operations = cast("Mapping[str, Mapping[str, object]]", startup.get("operations", {}))
            for name, values in operations.items():
                lines.append(
                    f"    {name}: worker count {values.get('worker_count', 'unavailable')}; "
                    f"process start call ms {values.get('start_call_ms', 'unavailable')}; "
                    f"first message wait ms {values.get('first_message_wait_ms', 'unavailable')}; "
                    f"result wait ms {values.get('result_wait_ms', 'unavailable')}."
                )
    return "\n".join(lines) + "\n"
