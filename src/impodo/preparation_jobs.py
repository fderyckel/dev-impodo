"""Session-scoped control-plane records for background data preparation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .recipes import (
    DataVersionPurpose,
    DataVersionState,
    WorkspaceResolution,
    require_uuid,
)


class PreparationJobStatus(StrEnum):
    """Lifecycle states visible to the browser."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_PREPARATION_JOB_STATUSES = frozenset(
    {PreparationJobStatus.QUEUED, PreparationJobStatus.RUNNING}
)
TERMINAL_PREPARATION_JOB_STATUSES = frozenset(
    {
        PreparationJobStatus.SUCCEEDED,
        PreparationJobStatus.REVIEW_REQUIRED,
        PreparationJobStatus.FAILED,
        PreparationJobStatus.CANCELLED,
    }
)


class PreparationPhase(StrEnum):
    """Stable, coarse phases suitable for non-technical progress reporting."""

    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    TRANSFORMING = "TRANSFORMING"
    PUBLISHING = "PUBLISHING"
    QUALITY = "QUALITY"
    NORMALIZING = "NORMALIZING"
    COMPLETE = "COMPLETE"


PHASE_LABELS: dict[PreparationPhase, str] = {
    PreparationPhase.QUEUED: "Waiting to start",
    PreparationPhase.VALIDATING: "Checking the saved setup",
    PreparationPhase.TRANSFORMING: "Preparing source rows",
    PreparationPhase.PUBLISHING: "Saving prepared data",
    PreparationPhase.QUALITY: "Running data checks",
    PreparationPhase.NORMALIZING: "Organizing changes for review",
    PreparationPhase.COMPLETE: "Preparation complete",
}


@dataclass(frozen=True, slots=True)
class PreparationWorkspace:
    """Registry-authorized Recipe/DataVersion identity captured before spawn."""

    recipe_id: str
    data_version_id: str
    data_version_number: int
    data_version_purpose: DataVersionPurpose

    def __post_init__(self) -> None:
        require_uuid(self.recipe_id, "recipe_id")
        require_uuid(self.data_version_id, "data_version_id")
        if self.data_version_number < 1:
            raise ValueError("Data version number is invalid")
        object.__setattr__(
            self,
            "data_version_purpose",
            DataVersionPurpose(self.data_version_purpose),
        )

    @classmethod
    def from_resolution(
        cls,
        resolution: WorkspaceResolution,
    ) -> "PreparationWorkspace":
        """Capture only an active workspace after registry authorization."""

        if resolution.data_version_state is not DataVersionState.ACTIVE:
            raise ValueError("Only the active data version can be prepared")
        return cls(
            recipe_id=resolution.recipe_id,
            data_version_id=resolution.data_version_id,
            data_version_number=resolution.data_version_number,
            data_version_purpose=resolution.data_version_purpose,
        )


@dataclass(frozen=True, slots=True)
class PreparationJob:
    """One preparation attempt and its latest in-memory progress snapshot."""

    job_id: str
    project_id: str
    project_name: str
    workspace: PreparationWorkspace
    status: PreparationJobStatus
    phase: PreparationPhase
    message: str
    completed_rows: int
    total_rows: int
    progress_percent: int
    attempt: int
    cancel_requested: bool
    requested_by_issuer: str
    requested_by_subject: str
    requested_by_display_name: str
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    result_run_id: str
    failure_code: str
    failure_message: str

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_PREPARATION_JOB_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_PREPARATION_JOB_STATUSES


def preparation_progress_percent(
    phase: PreparationPhase,
    *,
    completed_rows: int,
    total_rows: int,
) -> int:
    """Map exact row progress and coarse later phases to a monotonic percentage."""

    if phase is PreparationPhase.QUEUED:
        return 0
    if phase is PreparationPhase.VALIDATING:
        return 3
    if phase is PreparationPhase.TRANSFORMING:
        fraction = completed_rows / total_rows if total_rows > 0 else 0.0
        return min(55, 5 + round(50 * max(0.0, min(1.0, fraction))))
    return {
        PreparationPhase.PUBLISHING: 60,
        PreparationPhase.QUALITY: 72,
        PreparationPhase.NORMALIZING: 88,
        PreparationPhase.COMPLETE: 100,
    }[phase]
