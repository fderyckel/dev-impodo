"""Durable target-specific journal contracts for the practical load path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json
import re

from impodo.domain.shared.models import canonical_json_bytes


MAX_CREATE_BATCH_ROWS = 50
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class ExecutionRunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ExecutionRowStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_FLIGHT = "IN_FLIGHT"
    RETRY_READY = "RETRY_READY"
    COMMITTED = "COMMITTED"
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ExecutionRowAttempt:
    """One write-row journal projection without source values or credentials."""

    row_id: str
    dataset: str
    source_row: int
    target_model: str
    operation: str
    field_names: tuple[str, ...]
    proposed_external_id: str
    status: ExecutionRowStatus = ExecutionRowStatus.PLANNED
    attempt: int = 0
    odoo_id: int | None = None
    safe_error: str = ""
    schedule_component: int = -1
    transport_page: int = -1
    transport_batch: int = -1
    transport_phase: str = ""
    recovery_hash: str = ""

    def __post_init__(self) -> None:
        if (
            self.source_row < 1
            or self.attempt < 0
            or self.schedule_component < -1
            or self.transport_page < -1
            or self.transport_batch < -1
            or self.transport_phase not in {"", "CREATE", "UPDATE", "COMPLETION"}
            or (
                self.transport_phase
                and (
                    self.schedule_component < 0
                    or self.transport_page < 0
                    or self.transport_batch < 0
                )
            )
            or (
                self.recovery_hash
                and not _SHA256.fullmatch(self.recovery_hash)
            )
        ):
            raise ValueError("Execution row attempt is invalid")

    def to_json(self) -> str:
        payload = asdict(self)
        payload["status"] = self.status.value
        return canonical_json_bytes(payload).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "ExecutionRowAttempt":
        payload = json.loads(value)
        return cls(
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            target_model=str(payload["target_model"]),
            operation=str(payload["operation"]),
            field_names=tuple(str(item) for item in payload["field_names"]),
            proposed_external_id=str(payload.get("proposed_external_id", "")),
            status=ExecutionRowStatus(str(payload.get("status", "PLANNED"))),
            attempt=int(payload.get("attempt", 0)),
            odoo_id=(
                int(payload["odoo_id"])
                if payload.get("odoo_id") is not None
                else None
            ),
            safe_error=str(payload.get("safe_error", "")),
            schedule_component=int(payload.get("schedule_component", -1)),
            transport_page=int(payload.get("transport_page", -1)),
            transport_batch=int(payload.get("transport_batch", -1)),
            transport_phase=str(payload.get("transport_phase", "")),
            recovery_hash=str(payload.get("recovery_hash", "")),
        )


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    """One confirmed load attempt bound to an immutable execution snapshot."""

    run_id: str
    workspace_id: str
    snapshot_hash: str
    snapshot_root_hash: str
    preflight_run_id: str
    target_hash: str
    target_database: str
    batch_rows: int | None
    status: ExecutionRunStatus
    started_at: datetime
    started_by: str
    completed_at: datetime | None
    rows: tuple[ExecutionRowAttempt, ...]
    write_credential_binding_hash: str = ""
    write_principal_hash: str = ""
    write_permission_hash: str = ""
    write_context_hash: str = ""

    def __post_init__(self) -> None:
        """Reject partial or malformed non-secret execution identity evidence."""

        hashes = (
            self.write_credential_binding_hash,
            self.write_principal_hash,
            self.write_permission_hash,
            self.write_context_hash,
        )
        if any(hashes) and not all(_SHA256.fullmatch(value) for value in hashes):
            raise ValueError("Execution write-identity evidence is invalid")

    @property
    def committed_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.COMMITTED for item in self.rows)

    @property
    def failed_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.FAILED for item in self.rows)

    @property
    def partially_applied_count(self) -> int:
        return sum(
            item.status is ExecutionRowStatus.PARTIALLY_APPLIED
            for item in self.rows
        )

    @property
    def blocked_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.BLOCKED for item in self.rows)

    @property
    def unknown_count(self) -> int:
        return sum(
            item.status is ExecutionRowStatus.OUTCOME_UNKNOWN for item in self.rows
        )

    @property
    def planned_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.PLANNED for item in self.rows)

    @property
    def in_flight_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.IN_FLIGHT for item in self.rows)

    @property
    def retry_ready_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.RETRY_READY for item in self.rows)

    @property
    def active_component(self) -> int | None:
        active = {
            item.schedule_component
            for item in self.rows
            if item.status
            in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.IN_FLIGHT,
                ExecutionRowStatus.RETRY_READY,
                ExecutionRowStatus.PARTIALLY_APPLIED,
            }
            and item.schedule_component >= 0
        }
        return min(active) if active else None

    @property
    def active_batch(self) -> tuple[int, ...] | None:
        batches = {
            item.transport_batch
            for item in self.rows
            if item.status is ExecutionRowStatus.IN_FLIGHT
            and item.transport_batch >= 0
        }
        if not batches:
            return None
        return tuple(sorted(batches))

    @property
    def total_count(self) -> int:
        return len(self.rows)
