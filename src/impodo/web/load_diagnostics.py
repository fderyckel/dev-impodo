"""Validate and persist correlated load evidence for support exports.

Only typed operational facts cross this boundary. Terminal summaries have a
separate rotating log so request polling and long transport histories cannot
evict the only explanation of a recent failure.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.execution.load_observation import LoadObservation
from impodo.domain.execution.load_failure_reasons import LOAD_FAILURE_REASONS
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.write_diagnostics import OdooWriteDiagnostic

LOAD_EVENT_LOG = "impodo.loads.jsonl"
LOAD_SUMMARY_LOG = "impodo.load-summaries.jsonl"
LOAD_EVENTS = frozenset(
    {
        "attempt_started",
        "context",
        "enqueued",
        "worker_started",
        "stage_started",
        "stage_finished",
        "failure",
        "journal",
        "journal_batch",
        "transport_started",
        "transport_finished",
        "verification",
        "capture_failed",
        "terminal",
    }
)
LOAD_ACTIONS = frozenset(
    {
        "LOAD",
        "RECOVER",
        "VERIFY",
        "CHECK_RECOVERY",
        "STOP",
        "TRANSFER_LOAD",
        "TRANSFER_RECOVER",
        "TRANSFER_VERIFY",
    }
)
LOAD_STAGES = frozenset(
    {
        "submission",
        "worker",
        "submission_context",
        "submission_authorization",
        "submission_preview",
        "submission_credentials",
        "submission_enqueue",
        "target_authority",
        "close_prior_interrupted_load",
        "recovery_readback",
        "write",
        "verification",
        "execution_checks",
    }
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MAX_ATTEMPT_EVENTS = 2_000


def safe_uuid(value: object) -> str | None:
    """Accept only canonical internal UUIDs as support references."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _integer(value: object, maximum: int = 9_223_372_036_854_775_807) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _frames(value: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for raw in value[:8] if isinstance(value, list) else ():
        if not isinstance(raw, dict):
            continue
        module, function, line = raw.get("module"), raw.get("function"), raw.get("line")
        if (
            not isinstance(module, str)
            or not module.startswith("impodo/")
            or not re.fullmatch(r"impodo/[A-Za-z0-9_/]+\.py", module)
            or not (_PACKAGE_ROOT / module.removeprefix("impodo/")).is_file()
            or not isinstance(function, str)
            or not _NAME.fullmatch(function)
            or not _integer(line, 1_000_000)
        ):
            continue
        result.append({"module": module, "function": function, "line": line})
    return result


def sanitize_load_facts(value: object) -> dict[str, object]:
    """Apply the same closed schema to live capture and untrusted log input."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in (
        "attempt_id",
        "event_id",
        "request_id",
        "job_id",
        "execution_run_id",
        "transport_id",
        "previous_attempt_id",
    ):
        if (identifier := safe_uuid(value.get(key))) is not None:
            result[key] = identifier
    enums = {
        "action": LOAD_ACTIONS,
        "stage": LOAD_STAGES,
        "source_mode": {"FILE", "ODOO"},
        "connection_mode": {"LOCAL", "REMOTE"},
        "entry_point": {"AUTHORING", "RECIPE"},
        "projection": {"INITIAL", "FINAL", "OBSERVED"},
        "outcome": {
            "FAILED",
            "COMPLETED",
            "REJECTED",
            "UNKNOWN",
            "RETURNED",
            "SUCCEEDED",
            "DISCARDED",
        },
        "transport_phase": {"CREATE", "UPDATE", "COMPLETION"},
        "method": {"create", "load", "write"},
        "journal_status": {item.value for item in ExecutionRunStatus},
        "load_control_state": {
            "RUNNING",
            "PAUSED",
            "STOP_REQUESTED",
            "STOPPED",
            "FINISHED",
        },
    }
    for key, allowed in enums.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    for key in (
        "process_id",
        "batch_size",
        "batch_rows",
        "schedule_component",
        "transport_page",
        "transport_batch",
        "affected_rows",
        "omitted_group_count",
        "omitted_field_count",
        "omitted_event_count",
        "omitted_frame_count",
        "recorded_event_count",
        "workspace_revision",
    ):
        if _integer(value.get(key)):
            result[key] = value[key]
    if type(value.get("odoo_major")) is int and value.get("odoo_major") in {19, 20}:
        result["odoo_major"] = value["odoo_major"]
    for key in ("verification_complete", "cause_chain_truncated"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    build = value.get("producer_build_id")
    if isinstance(build, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", build):
        result["producer_build_id"] = build
    timestamp = value.get("recorded_at")
    if isinstance(timestamp, str) and len(timestamp) <= 40:
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is not None:
                result["recorded_at"] = parsed.astimezone(UTC).isoformat()
        except ValueError:
            pass
    for key in ("model", "exception_class"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _NAME.fullmatch(candidate):
            result[key] = candidate
    if isinstance(value.get("pause_reason"), str) and value["pause_reason"] in {
        "",
        "USER_STOP",
        "CONSECUTIVE_REJECTIONS",
        "REJECTION_RATE",
        "ISOLATION_BUDGET",
    }:
        result["pause_reason"] = value["pause_reason"]
    fields = value.get("fields")
    if isinstance(fields, list):
        result["fields"] = [
            field
            for field in fields[:20]
            if isinstance(field, str) and _NAME.fullmatch(field)
        ]
    states = value.get("row_states")
    if isinstance(states, dict):
        result["row_states"] = {
            state.value: states[state.value]
            for state in ExecutionRowStatus
            if _integer(states.get(state.value))
        }
    causes = value.get("causes")
    if isinstance(causes, list):
        safe_causes = []
        for raw in causes[:5]:
            if not isinstance(raw, dict):
                continue
            code = raw.get("code")
            safe: dict[str, object] = {
                "code": code
                if isinstance(code, str) and code in LOAD_FAILURE_REASONS
                else "LOAD_UNCLASSIFIED",
                "frames": _frames(raw.get("frames")),
            }
            name = raw.get("exception_class")
            if isinstance(name, str) and _NAME.fullmatch(name):
                safe["exception_class"] = name
            if _integer(raw.get("omitted_frame_count")):
                safe["omitted_frame_count"] = raw["omitted_frame_count"]
            safe_causes.append(safe)
        result["causes"] = safe_causes
    diagnostics = value.get("diagnostics")
    if isinstance(diagnostics, list):
        safe_diagnostics = []
        for raw in diagnostics[:40]:
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            affected = payload.pop("affected_rows", None)
            payload.pop("message_fingerprint", None)
            try:
                diagnostic = OdooWriteDiagnostic.from_dict(payload)
            except (ValueError, TypeError, KeyError):
                continue
            safe = diagnostic.to_dict()
            safe.pop("message_fingerprint", None)
            if _integer(affected):
                safe["affected_rows"] = affected
            safe_diagnostics.append(safe)
        result["diagnostics"] = safe_diagnostics
    if isinstance(value.get("reason"), str) and value.get("reason") in {
        "JOURNAL_UNAVAILABLE",
        "QUEUED_SHUTDOWN",
        "SUBMISSION_REJECTED",
        "CAPTURE_FAILED",
    }:
        result["reason"] = value["reason"]
    duration = value.get("duration_ms")
    if (
        isinstance(duration, (float, int))
        and not isinstance(duration, bool)
        and 0 <= duration <= 86_400_000
    ):
        result["duration_ms"] = round(duration, 3)
    return result


def sanitize_load_record(value: object) -> dict[str, object] | None:
    """Reject malformed envelopes and resanitize every exported load record."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("event"), str)
        or value.get("event") not in LOAD_EVENTS
    ):
        return None
    facts = sanitize_load_facts(value)
    timestamp = value.get("recorded_at")
    if "attempt_id" not in facts or not isinstance(timestamp, str):
        return None
    try:
        if datetime.fromisoformat(timestamp).tzinfo is None:
            return None
    except ValueError:
        return None
    record = {
        "schema_version": 1,
        "event": value["event"],
        "recorded_at": timestamp,
        **facts,
    }
    if value["event"] == "terminal":
        for key in ("context", "failure", "journal", "verification", "last_transport", "capture_failed"):
            if isinstance(value.get(key), dict):
                record[key] = sanitize_load_facts(value[key])
        if isinstance(value.get("transport_observation"), str) and value.get(
            "transport_observation"
        ) in {"NOT_STARTED", "STARTED", "UNKNOWN"}:
            record["transport_observation"] = value["transport_observation"]
    return record


def new_load_observation(
    write_event: Callable[[dict[str, object]], None],
    write_summary: Callable[[dict[str, object]], None],
    *,
    action: str,
    request_id: str,
) -> LoadObservation:
    """Create a bounded attempt whose terminal evidence survives log floods."""
    attempt_id = str(uuid4())
    common: dict[str, object] = {
        "attempt_id": attempt_id,
        "action": action,
        "request_id": request_id,
        "producer_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "process_id": os.getpid(),
    }
    retained: dict[str, object] = {"transport_observation": "NOT_STARTED"}
    lock = RLock()
    event_count = 0
    omitted = 0

    def sink(event: str, facts: Mapping[str, object]) -> None:
        nonlocal event_count, omitted
        with lock:
            safe = sanitize_load_facts({**common, **facts, "event_id": str(uuid4()), "recorded_at": datetime.now(UTC).isoformat()})
            record = {"event": event, **safe}
            for key in (
                "job_id",
                "execution_run_id",
                "entry_point",
                "workspace_revision",
                "previous_attempt_id",
            ):
                if key in safe:
                    common[key] = safe[key]
            if event in {"context", "failure", "journal", "verification", "capture_failed"}:
                retained[event] = safe
            if event == "transport_started":
                retained["transport_observation"] = "STARTED"
                retained["last_transport"] = safe
            if event == "transport_finished":
                previous_transport = retained.get("last_transport", {})
                retained["last_transport"] = (
                    {**previous_transport, **safe}
                    if isinstance(previous_transport, dict)
                    else safe
                )
                if safe.get("outcome") == "UNKNOWN":
                    retained["transport_observation"] = "UNKNOWN"
            if event_count < MAX_ATTEMPT_EVENTS or event in {
                "failure",
                "terminal",
                "capture_failed",
            }:
                write_event(record)
                event_count += 1
            else:
                omitted += 1
            if event == "terminal":
                write_summary(
                    {
                        **record,
                        **retained,
                        "omitted_event_count": omitted,
                        "recorded_event_count": event_count,
                    }
                )

    observer = LoadObservation(attempt_id, sink)
    observer.emit("attempt_started")
    return observer
