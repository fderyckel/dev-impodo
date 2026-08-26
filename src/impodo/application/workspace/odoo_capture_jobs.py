"""Small session-scoped control records for browser Odoo captures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.application.workspace.access import WorkspaceAccessContext


class OdooCaptureJobStatus(StrEnum):
    """Lifecycle states exposed by the browser progress endpoint."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OdooCapturePhase(StrEnum):
    """Coarse phases that do not disclose captured business values."""

    QUEUED = "QUEUED"
    VERIFYING = "VERIFYING"
    READING = "READING"
    FINALIZING = "FINALIZING"
    PUBLISHING = "PUBLISHING"
    COMPLETE = "COMPLETE"


CAPTURE_PHASE_LABELS: dict[OdooCapturePhase, str] = {
    OdooCapturePhase.QUEUED: "Waiting to start",
    OdooCapturePhase.VERIFYING: "Verifying the saved Odoo connection",
    OdooCapturePhase.READING: "Reading the selected Odoo records",
    OdooCapturePhase.FINALIZING: "Checking the complete capture",
    OdooCapturePhase.PUBLISHING: "Making the frozen version current",
    OdooCapturePhase.COMPLETE: "Odoo records frozen",
}


@dataclass(frozen=True, slots=True)
class OdooCaptureProgress:
    """Bounded counters reported from the one-pass publication stream."""

    phase: OdooCapturePhase
    completed_rows: int
    total_rows: int
    page_count: int
    response_bytes: int
    normalized_bytes: int


@dataclass(frozen=True, slots=True)
class OdooCaptureJob:
    """One browser capture attempt; values and credentials never enter it."""

    job_id: str
    access_context: WorkspaceAccessContext
    workspace_id: str
    migration_project_name: str
    status: OdooCaptureJobStatus
    phase: OdooCapturePhase
    message: str
    completed_rows: int
    total_rows: int
    page_count: int
    response_bytes: int
    normalized_bytes: int
    progress_percent: int
    attempt: int
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    manifest_id: str
    failure_message: str

    @property
    def active(self) -> bool:
        return self.status in {
            OdooCaptureJobStatus.QUEUED,
            OdooCaptureJobStatus.RUNNING,
        }

    @property
    def terminal(self) -> bool:
        return not self.active


def odoo_capture_progress_percent(progress: OdooCaptureProgress) -> int:
    """Map honest row progress and bounded final phases monotonically."""

    if progress.phase is OdooCapturePhase.QUEUED:
        return 0
    if progress.phase is OdooCapturePhase.VERIFYING:
        return 5
    if progress.phase is OdooCapturePhase.READING:
        fraction = (
            progress.completed_rows / progress.total_rows
            if progress.total_rows > 0
            else 0.0
        )
        return min(80, 8 + round(72 * max(0.0, min(1.0, fraction))))
    return {
        OdooCapturePhase.FINALIZING: 86,
        OdooCapturePhase.PUBLISHING: 94,
        OdooCapturePhase.COMPLETE: 100,
    }[progress.phase]
