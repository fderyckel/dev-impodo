"""Session-scoped control-plane records for background data preparation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .build_contract import ApplicationBuildContract
from .data_versions import DataVersion, DataVersionPurpose, DataVersionState
from .migration_foundation import MigrationFoundationError, require_uuid
from .migration_runs import MigrationRun
from .migration_workspaces import MigrationWorkspace, MigrationWorkspaceState


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

NON_RETRYABLE_PREPARATION_FAILURE_CODES = frozenset(
    {
        "IMPODO_BUILD_CHANGED",
        "WorkspaceStateCompatibilityError",
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
    """Project-owned identities authorized before a worker is spawned."""

    project_id: str
    data_version_id: str
    data_version_number: int
    data_version_purpose: DataVersionPurpose
    migration_run_id: str
    workspace_id: str
    recipe_application_id: str | None = None

    def __post_init__(self) -> None:
        require_uuid(self.project_id, "project_id")
        require_uuid(self.data_version_id, "data_version_id")
        require_uuid(self.migration_run_id, "migration_run_id")
        require_uuid(self.workspace_id, "workspace_id")
        if self.recipe_application_id is not None:
            require_uuid(self.recipe_application_id, "recipe_application_id")
        if self.data_version_number < 1:
            raise ValueError("Data version number is invalid")
        object.__setattr__(
            self,
            "data_version_purpose",
            DataVersionPurpose(self.data_version_purpose),
        )

    @classmethod
    def from_context(
        cls,
        workspace: MigrationWorkspace,
        data_version: DataVersion,
        run: MigrationRun,
    ) -> "PreparationWorkspace":
        """Capture one open workspace over one accepted DataVersion."""

        if workspace.state is not MigrationWorkspaceState.OPEN:
            raise MigrationFoundationError(
                "Only an open MigrationWorkspace can be prepared"
            )
        if data_version.state is not DataVersionState.FROZEN:
            raise MigrationFoundationError(
                "Freeze the source datasets before preparing data"
            )
        if (
            workspace.project_id != data_version.project_id
            or workspace.project_id != run.project_id
            or workspace.data_version_id != data_version.data_version_id
            or run.data_version_id != data_version.data_version_id
            or workspace.migration_run_id != run.migration_run_id
        ):
            raise MigrationFoundationError(
                "The Project, DataVersion, run, and workspace do not match"
            )
        return cls(
            project_id=workspace.project_id,
            data_version_id=data_version.data_version_id,
            data_version_number=data_version.version_number,
            data_version_purpose=data_version.purpose,
            migration_run_id=run.migration_run_id,
            workspace_id=workspace.workspace_id,
            recipe_application_id=workspace.recipe_application_id,
        )


@dataclass(frozen=True, slots=True)
class PreparationJob:
    """One preparation attempt and its latest in-memory progress snapshot."""

    job_id: str
    workspace_id: str
    migration_project_name: str
    build_contract: ApplicationBuildContract
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

    @property
    def retry_allowed(self) -> bool:
        """Return whether repeating the same saved request can make progress."""

        return (
            self.status in {PreparationJobStatus.FAILED, PreparationJobStatus.CANCELLED}
            and self.failure_code not in NON_RETRYABLE_PREPARATION_FAILURE_CODES
        )


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
