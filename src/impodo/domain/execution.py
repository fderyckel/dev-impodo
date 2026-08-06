"""Durable target-specific journal contracts for the practical load path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json

from ..models import canonical_json_bytes


class ExecutionRunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ExecutionRowStatus(StrEnum):
    PLANNED = "PLANNED"
    COMMITTED = "COMMITTED"
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
        )


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    """One confirmed load attempt bound to an immutable execution snapshot."""

    run_id: str
    project_id: str
    snapshot_hash: str
    snapshot_root_hash: str
    preflight_run_id: str
    target_hash: str
    target_database: str
    status: ExecutionRunStatus
    started_at: datetime
    started_by: str
    completed_at: datetime | None
    rows: tuple[ExecutionRowAttempt, ...]

    @property
    def committed_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.COMMITTED for item in self.rows)

    @property
    def failed_count(self) -> int:
        return sum(item.status is ExecutionRowStatus.FAILED for item in self.rows)

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
    def total_count(self) -> int:
        return len(self.rows)
