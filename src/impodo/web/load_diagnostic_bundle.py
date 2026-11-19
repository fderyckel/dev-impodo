"""Build bounded, evidence-backed load explanations from local logs.

This exporter performs no workspace database reads or Odoo calls. It validates
the dedicated histories again, preserves terminal evidence independently of
event limits, and states when legacy or truncated capture cannot prove a fact.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from impodo.domain.execution.load_failure_reasons import LOAD_FAILURE_REASONS
from impodo.domain.execution.models import ExecutionRowAttempt
from impodo.domain.execution.operator_errors import OperatorErrorReceipt
from impodo.domain.execution.write_diagnostics import OdooWriteDiagnostic
from impodo.web.load_diagnostics import (
    LOAD_EVENT_LOG,
    LOAD_SUMMARY_LOG,
    sanitize_load_record,
)

MAX_LOAD_ATTEMPTS = 100
MAX_LOAD_EVENTS = 10_000
MAX_LOAD_AGE_DAYS = 30
MAX_LOAD_FILES = 32
MAX_LINE_BYTES = 64 * 1024
PRIVATE_REJECTION_MEMBER = "odoo-rejection.json"


@dataclass(frozen=True, slots=True, repr=False)
class DiagnosticRejectionEvidence:
    """Carry only the saved receipt for the operator's selected rejection."""

    workspace_id: str
    execution_run_id: str
    row: ExecutionRowAttempt
    receipt: OperatorErrorReceipt

    def to_json(self) -> str:
        """Export the selected values and preserve the message's batch attribution."""
        index = self.receipt.row_ids.index(self.row.row_id)
        detail = self.receipt.detail
        batch_size = len(self.receipt.row_ids)
        messages = [
            {
                "explanation": message.text,
                "reported_fields": message.fields,
                "applies_to_selected_record": batch_size == 1 or message.row_from == message.row_to == index,
                "first_batch_position": message.row_from + 1 if message.row_from is not None else None,
                "last_batch_position": message.row_to + 1 if message.row_to is not None else None,
            }
            for message in detail.messages
            if message.row_from is None or (
                message.row_to is not None and message.row_from <= index <= message.row_to
            )
        ]
        diagnostic = self.row.diagnostic
        return json.dumps(
            {
                "rejection_schema_version": 1,
                "workspace_id": self.workspace_id,
                "execution_run_id": self.execution_run_id,
                "snapshot_hash": self.receipt.snapshot_hash,
                "execution_row_id": self.row.row_id,
                "dataset": self.row.dataset,
                "source_row": self.row.source_row,
                "target_model": self.row.target_model,
                "operation": self.row.operation,
                "write_failure_code": diagnostic.code.value if diagnostic else None,
                "http_status": diagnostic.http_status if diagnostic else None,
                "odoo_exception": diagnostic.error_type.value if diagnostic and diagnostic.error_type else None,
                "row_attempt_number": self.row.attempt,
                "transport_batch": self.row.transport_batch,
                "transport_batch_size": batch_size,
                "selected_batch_position": index + 1,
                "odoo_messages": messages,
                "submitted_values_for_selected_record": detail.submitted[index],
                "detail_truncated": detail.truncated,
                "detail_withheld": detail.withheld,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


def _read_history(
    directory: Path, name: str, coverage: dict[str, Any]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    paths = sorted(directory.glob(name + "*"))
    coverage["omitted_file_count"] += max(0, len(paths) - MAX_LOAD_FILES)
    for path in paths[:MAX_LOAD_FILES]:
        if not path.is_file() or path.name.endswith(".lock"):
            continue
        try:
            with path.open("rb") as stream:
                while raw := stream.readline(MAX_LINE_BYTES + 1):
                    if len(raw) > MAX_LINE_BYTES:
                        coverage["oversize_record_count"] += 1
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(MAX_LINE_BYTES + 1)
                        continue
                    try:
                        record = sanitize_load_record(json.loads(raw))
                    except (ValueError, UnicodeDecodeError, TypeError):
                        record = None
                    if record is None:
                        coverage["malformed_record_count"] += 1
                    else:
                        records.append(record)
        except OSError:
            coverage["unreadable_file_count"] += 1
    records.sort(key=lambda item: item["recorded_at"])
    return records


def _unknowns(attempt: dict[str, Any]) -> list[str]:
    missing = []
    if (
        not attempt.get("failure", {}).get("causes")
        and attempt.get("outcome") == "FAILED"
    ):
        missing.append(
            "The cause was not recorded; no business explanation can be established."
        )
    if not attempt.get("context", {}).get("odoo_major"):
        missing.append("The verified Odoo major was not recorded for this attempt.")
    if not attempt.get("journal"):
        missing.append(
            "No durable journal projection was captured. Journal existence and saved row outcomes are unavailable."
        )
    if attempt.get("capture_failed") or (
        attempt.get("journal") and attempt["journal"].get("projection") != "FINAL"
    ):
        missing.append(
            "Final journal evidence is unavailable. Displayed totals describe the earlier captured snapshot, not the final write outcome."
        )
    if not attempt.get("verification"):
        missing.append("No verification result was captured.")
    if attempt.get("terminal_missing"):
        missing.append(
            "No terminal event was retained. The attempt may still be active or may have been interrupted."
        )
    if attempt.get("omitted_event_count", 0) or attempt.get(
        "omitted_recorded_event_count", 0
    ):
        missing.append(
            "Some attempt events were omitted by retention limits; the terminal summary has priority."
        )
    for cause in attempt.get("failure", {}).get("causes", []):
        if cause.get("code") == "LOAD_UNCLASSIFIED":
            missing.append(
                "An exception has no classified reason. Its message was withheld; inspect its source frames and producer build."
            )
            break
    for diagnostic in attempt.get("journal", {}).get("diagnostics", []):
        if diagnostic.get("operator_detail_status") in {"UNAVAILABLE", "CAPTURE_FAILED", "WITHHELD", "TRUNCATED"}:
            missing.append("Private Odoo explanation coverage is incomplete; the saved authorized view identifies missing, withheld, or truncated detail.")
        if diagnostic.get("code") == "ODOO_VALUE_REJECTED" and not diagnostic.get("fields"):
            missing.append(
                "No rejected field was retained. The validation code alone does not identify a cause; inspect the authorized private Odoo error detail when available."
            )
        if (
            diagnostic.get("code") == "ODOO_WRITE_OUTCOME_UNKNOWN"
            and not diagnostic.get("uncertainty_reason")
            and diagnostic.get("http_status") is None
        ):
            missing.append(
                "The uncertain write has no recorded HTTP status or response failure detail."
            )
        if diagnostic.get("code") == "ODOO_WRITE_REJECTED":
            missing.append(
                "Odoo supplied no recognized specific rejection reason. Exact server text is outside the routine bundle."
            )
            break
    return missing


def _next_action(attempt: dict[str, Any]) -> str:
    journal = attempt.get("journal", {})
    states = journal.get("row_states", {})
    if (
        attempt.get("transport_observation") == "UNKNOWN"
        or states.get("IN_FLIGHT", 0)
        or states.get("OUTCOME_UNKNOWN", 0)
        or journal.get("journal_status") in {"RUNNING", "OUTCOME_UNKNOWN"}
    ):
        return "Open the saved load outcome and use recovery read-back before continuing. Do not resend an uncertain write."
    if attempt.get("transport_observation") == "NOT_STARTED":
        return "Resolve the recorded blocking condition and review the current comparison before confirming another load. This attempt did not start a write call; prior attempts require their own outcome review."
    if attempt.get("transport_observation") is None or not journal:
        return "Review the saved load outcome and establish write results through read-back before attempting another load. The bundle cannot establish that retrying is safe."
    if (
        states.get("FAILED", 0)
        or states.get("BLOCKED", 0)
        or states.get("PARTIALLY_APPLIED", 0)
    ):
        return "Review rejected records and unfinished relationships in the saved outcome. Use the governed correction or continuation workflow; preserve previously accepted records."
    if not attempt.get("verification", {}).get("verification_complete"):
        return "Run verification from the saved outcome. Returned write calls are not proof that the completed data is verified."
    return "Review the verified saved outcome. No new load is required for these already verified results."


def _diagnosis(
    attempts: list[dict[str, Any]],
    legacy_failures: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> str:
    lines = [
        "# Load diagnosis",
        "",
        "This summary reports recorded evidence. It does not authorize writes or retries.",
        "",
    ]
    if coverage.get("private_rejection_member"):
        lines += [
            "",
            f"Open `{PRIVATE_REJECTION_MEMBER}` for the selected rejection's private Odoo explanation and submitted values. Use the execution run and row identifiers in that file to identify the saved record.",
        ]
    relevant = [
        attempt
        for attempt in attempts
        if attempt.get("outcome") == "FAILED"
        or attempt.get("failure")
        or any(
            attempt.get("journal", {}).get("row_states", {}).get(state, 0)
            for state in (
                "FAILED",
                "BLOCKED",
                "PARTIALLY_APPLIED",
                "OUTCOME_UNKNOWN",
                "IN_FLIGHT",
            )
        )
        or attempt.get("terminal_missing")
    ]
    if not attempts:
        lines += ["No correlated load attempt was recorded by this capture.", ""]
        if coverage.get("requested_attempt_retained") is False:
            lines += [
                "The requested support reference is not present in the retained history. Export the general bundle to inspect other attempts; do not attribute another attempt's error to this reference.",
                "",
            ]
            legacy_failures = []
        if coverage.get("requested_execution_run_retained") is False:
            lines += [
                "The selected execution run has no retained attempt logs. Use the saved rejection evidence for this record.",
                "",
            ]
            legacy_failures = []
        for item in legacy_failures[-5:]:
            lines += [
                f"Legacy event at {item.get('recorded_at', 'unknown time')}: stage `{item.get('stage', 'unknown')}`, exception `{item.get('exception_class', 'unknown')}`. See `recent-failures.json` or `diagnostics.jsonl` at that timestamp."
            ]
        lines += [
            "",
            "The cause, target version, and extent of Odoo writes are unknown. Legacy stage names and HTTP success responses do not prove a business outcome.",
            "",
            "Next action: review the saved load outcome and use read-back to establish uncertain results before another write.",
        ]
    else:
        attempt = (relevant or attempts)[-1]
        reference = attempt["attempt_id"]
        lines += [
            f"Support reference: `{reference}`.",
            f"Evidence: `load-attempts.json`, attempt `{reference}`; its event IDs refer to `load-events.jsonl` or retained terminal evidence.",
            "",
            f"Action `{attempt.get('action', 'unknown')}`. Worker or request status `{attempt.get('outcome', 'TERMINAL_NOT_RECORDED')}`.",
            f"Producer build: `{attempt.get('producer_build_id', 'not recorded')}`.",
            "",
            "## Why the load stopped",
            "",
        ]
        failure = attempt.get("failure", {})
        causes = failure.get("causes", [])
        transport = attempt.get("last_transport", {})
        if failure.get("model"):
            lines += [
                f"Context while preparing the write: model `{failure['model']}`, fields `{', '.join(failure.get('fields', []))}`. This identifies the attempted field, not a server-reported faulty source row."
            ]
        if causes:
            lines += [
                f"Failure evidence: `{failure.get('event_id', 'retained failure')}`, stage `{failure.get('stage', 'unknown')}`."
            ]
            for cause in causes:
                code = cause.get("code", "LOAD_UNCLASSIFIED")
                lines += [
                    f"- `{code}`: {LOAD_FAILURE_REASONS.get(code, LOAD_FAILURE_REASONS['LOAD_UNCLASSIFIED'])}"
                ]
                if cause.get("failure_category"):
                    lines += [
                        f"  Read failure category: `{cause['failure_category']}`. Attempts: {cause.get('attempt_count', 'not captured')}."
                    ]
                for frame in cause.get("frames", [])[-3:]:
                    lines += [
                        f"  Source: `{frame['module']}:{frame['line']}`, function `{frame['function']}`."
                    ]
        elif transport.get("outcome") == "UNKNOWN":
            lines += [
                f"Impodo held back further uploads after a write for {transport.get('batch_size', 'an unrecorded number of')} records ended without a confirmed result."
            ]
            if not attempt.get("journal", {}).get("diagnostics"):
                lines.extend(
                    OdooWriteDiagnostic.from_dict(raw).summary
                    for raw in transport.get("diagnostics", [])
                )
        else:
            lines += [
                "No fatal exception reason was recorded. Review the journal rejection evidence below; a completed worker can still contain rejected rows."
            ]
        lines += [
            "",
            "## What happened in Odoo",
            "",
            f"Write-call observation: `{attempt.get('transport_observation', 'UNKNOWN')}`.",
        ]
        journal = attempt.get("journal", {})
        if journal:
            lines += [
                f"Journal evidence: `{journal.get('event_id', 'retained journal')}`, status `{journal.get('journal_status', 'unknown')}`.",
                "Recorded row states: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in journal.get("row_states", {}).items()
                )
                + ".",
            ]
            for raw in journal.get("diagnostics", []):
                diagnostic = OdooWriteDiagnostic.from_dict(raw)
                lines += [
                    f"- `{diagnostic.model}` / `{diagnostic.method}`: {diagnostic.summary} Affected attempts: {raw.get('affected_rows', 'not captured')}."
                ]
            if transport.get("outcome") == "REJECTED" and transport.get("fields"):
                fields = ", ".join(transport["fields"])
                lines += [
                    f"Recorded fields sent in the last rejected request: `{fields}`. These fields describe the request. Fields named by Odoo are recorded separately."
                ]
            if not coverage.get("private_rejection_member") and any(
                item.get("operator_detail_status") in {"AVAILABLE", "TRUNCATED"}
                for item in journal.get("diagnostics", [])
            ):
                lines += [
                    "For the saved explanation and submitted values, open Review Odoo error for a rejected record and select Create diagnostic bundle with Odoo error."
                ]
        else:
            lines += [
                "The durable row outcomes were not captured. Zero reported commits must not be treated as proof of no changes."
            ]
        lines += [
            "",
            "## Next action",
            "",
            _next_action(attempt),
            "",
            "## Evidence gaps",
            "",
        ]
        lines += ["- " + item for item in attempt["evidence_gaps"]] or [
            "No classified evidence gaps were recorded."
        ]
    lines += [
        "",
        "## Capture coverage",
        "",
        f"Retained attempts: {coverage['attempt_count']}. Omitted events: {coverage['omitted_event_count']}. Unreadable files: {coverage['unreadable_file_count']}. Malformed records: {coverage['malformed_record_count']}.",
        "Routine diagnostics omit source values, credentials, arbitrary server messages, stack locals, and absolute paths.",
        "",
    ]
    return "\n".join(lines)


def load_bundle_members(
    directory: str | Path,
    legacy_records: list[dict[str, Any]],
    *,
    attempt_id: str | None = None,
    rejection: DiagnosticRejectionEvidence | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return validated load collections and an explicit coverage manifest."""
    coverage: dict[str, Any] = {
        "malformed_record_count": 0,
        "oversize_record_count": 0,
        "unreadable_file_count": 0,
        "omitted_file_count": 0,
        "max_attempts": MAX_LOAD_ATTEMPTS,
        "max_events": MAX_LOAD_EVENTS,
        "max_age_days": MAX_LOAD_AGE_DAYS,
        "selection": "Recent terminal summaries and correlated attempt events, independent of request traffic.",
    }
    events = _read_history(Path(directory), LOAD_EVENT_LOG, coverage)
    summaries = _read_history(Path(directory), LOAD_SUMMARY_LOG, coverage)
    cutoff = datetime.now(UTC) - timedelta(days=MAX_LOAD_AGE_DAYS)
    retained = [
        item
        for item in events + summaries
        if datetime.fromisoformat(item["recorded_at"]) >= cutoff
    ]
    coverage["expired_record_count"] = len(events) + len(summaries) - len(retained)
    by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in retained:
        by_attempt[item["attempt_id"]].append(item)
    ordered = sorted(
        by_attempt, key=lambda key: max(item["recorded_at"] for item in by_attempt[key])
    )
    selected = ordered[-MAX_LOAD_ATTEMPTS:]
    if attempt_id is not None:
        selected = [attempt_id] if attempt_id in by_attempt else []
        coverage["requested_attempt_retained"] = bool(selected)
    if rejection is not None:
        selected = [
            key for key in ordered
            if any(item.get("execution_run_id") == rejection.execution_run_id for item in by_attempt[key])
        ][-MAX_LOAD_ATTEMPTS:]
        coverage["requested_execution_run_retained"] = bool(selected)
        coverage["private_rejection_member"] = PRIVATE_REJECTION_MEMBER
    coverage["omitted_attempt_count"] = max(0, len(ordered) - len(selected))
    attempts = []
    selected_events = [
        item
        for item in events
        if item["attempt_id"] in selected
        and datetime.fromisoformat(item["recorded_at"]) >= cutoff
    ]
    omitted_export_events = max(0, len(selected_events) - MAX_LOAD_EVENTS)
    selected_events = selected_events[-MAX_LOAD_EVENTS:]
    for key in selected:
        history = sorted(by_attempt[key], key=lambda item: item["recorded_at"])
        terminal = next(
            (
                item
                for item in reversed(history)
                if item["event"] == "terminal" and "transport_observation" in item
            ),
            None,
        )
        attempt = dict(terminal or history[0])
        attempt["first_observed_at"], attempt["last_observed_at"] = (
            history[0]["recorded_at"],
            history[-1]["recorded_at"],
        )
        attempt["terminal_missing"] = terminal is None
        for item in history:
            for name in ("job_id", "execution_run_id", "entry_point"):
                if name in item:
                    attempt[name] = item[name]
            if terminal is None and item["event"] in {
                "context",
                "failure",
                "journal",
                "verification",
            }:
                attempt[item["event"]] = item
            if terminal is None:
                for name in ("runtime", "local_phase_metrics", "stage_performance", "local_phase_checkpoint_count"):
                    if name in item:
                        attempt[name] = item[name]
        if terminal is None:
            attempt.pop("outcome", None)
            attempt["transport_observation"] = "UNKNOWN"
        runtime = attempt.get("runtime")
        runtime_coverage = runtime.get("coverage", {}) if isinstance(runtime, dict) else {}
        attempt["performance_coverage"] = {
            "runtime": "available" if runtime_coverage.get("available_fields") else "unavailable",
            "local_phase_metrics": "available" if attempt.get("local_phase_metrics") else "unavailable",
            "stage_performance": "available" if attempt.get("stage_performance") else "unavailable",
            "terminal_aggregate_present": terminal is not None and ("local_phase_metrics" in attempt or "stage_performance" in attempt),
            "local_phase_checkpoint_limit": 16,
            "local_phase_checkpoint_interval_calls": 128,
            "interrupted_aggregate": "last retained checkpoint only; later local work is unavailable" if terminal is None else "terminal aggregate",
            "phase_timings": "inclusive; parent and child measurements overlap",
            "thread_cpu": "coarse stages observe non-exclusive context-thread CPU and exclude worker-thread CPU; local phases observe their owning thread",
            "outcome_persistence": "journal_after_call can include locally blocked rows without a remote call",
            "security_storage_power_attribution": "unavailable",
        }
        actual_events = sum(item["attempt_id"] == key for item in selected_events)
        attempt["omitted_recorded_event_count"] = max(
            0, attempt.get("recorded_event_count", actual_events) - actual_events
        )
        attempt["evidence_gaps"] = _unknowns(attempt)
        attempts.append(attempt)
    coverage["attempt_count"] = len(attempts)
    coverage["omitted_export_event_count"] = omitted_export_events
    coverage["omitted_event_count"] = sum(
        item.get("omitted_event_count", 0) + item.get("omitted_recorded_event_count", 0)
        for item in attempts
    )
    coverage["event_count"] = len(selected_events)
    coverage["first_recorded_at"] = (
        selected_events[0]["recorded_at"] if selected_events else None
    )
    coverage["last_recorded_at"] = (
        selected_events[-1]["recorded_at"] if selected_events else None
    )
    legacy_failures = [
        item
        for item in legacy_records
        if item.get("operation") == "odoo_load" and item.get("exception_class")
    ]
    members = {
        "load-attempts.json": json.dumps(attempts, indent=2, sort_keys=True) + "\n",
        "load-events.jsonl": "".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in selected_events
        ),
        "diagnosis.md": _diagnosis(attempts, legacy_failures, coverage),
    }
    if rejection is not None:
        members[PRIVATE_REJECTION_MEMBER] = rejection.to_json()
    return members, coverage
