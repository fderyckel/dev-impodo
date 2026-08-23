"""Small session-scoped control records for confirmed Odoo loads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LoadJobStatus(StrEnum):
    """Lifecycle states exposed by the browser progress endpoint."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class LoadPhase(StrEnum):
    """Coarse phases that make remote-write progress understandable."""

    QUEUED = "QUEUED"
    CHECKING_TARGET = "CHECKING_TARGET"
    WRITING = "WRITING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"


LOAD_PHASE_LABELS: dict[LoadPhase, str] = {
    LoadPhase.QUEUED: "Waiting to start",
    LoadPhase.CHECKING_TARGET: "Checking the Odoo connection",
    LoadPhase.WRITING: "Sending records to Odoo",
    LoadPhase.VERIFYING: "Verifying the completed records in Odoo",
    LoadPhase.COMPLETE: "Odoo load finished",
}


@dataclass(frozen=True, slots=True)
class LoadJob:
    """One browser load attempt; business values and credentials stay out."""

    job_id: str
    project_id: str
    project_name: str
    target_database: str
    target_server: str
    target_environment: str
    status: LoadJobStatus
    phase: LoadPhase
    message: str
    total_rows: int
    completed_rows: int
    created_count: int
    updated_count: int
    attention_count: int
    relationship_pending_count: int
    progress_percent: int
    execution_run_id: str
    verification_complete: bool
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    failure_message: str

    @property
    def active(self) -> bool:
        return self.status in {LoadJobStatus.QUEUED, LoadJobStatus.RUNNING}

    @property
    def terminal(self) -> bool:
        return not self.active

    @property
    def not_attempted_count(self) -> int:
        return max(0, self.total_rows - self.completed_rows)
