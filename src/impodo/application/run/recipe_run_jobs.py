"""Run long Recipe Odoo checks without holding the browser request open."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from impodo.application.shared.serial_job_runner import (
    SerialJobRunner,
    SerialJobRunnerStoppedError,
)


class RecipeRunJobStatus(StrEnum):
    """Lifecycle states exposed to the local browser."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RecipeRunJobKind(StrEnum):
    """Long-running Recipe run tasks that share one progress surface."""

    ODOO_CHECK = "ODOO_CHECK"
    TARGET_MATCH_REVIEW = "TARGET_MATCH_REVIEW"


class RecipeRunJobPhase(StrEnum):
    """Stable, data-manager-facing progress phases."""

    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    READING_ODOO = "READING_ODOO"
    CHECKING_FIELDS = "CHECKING_FIELDS"
    SUPPORTING_VALUES = "SUPPORTING_VALUES"
    MATERIALIZING = "MATERIALIZING"
    TARGET_MATCHES = "TARGET_MATCHES"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class RecipeRunJobProgress:
    """One honest progress checkpoint emitted by governed work."""

    phase: RecipeRunJobPhase
    message: str
    progress_percent: int
    completed_units: int = 0
    total_units: int = 0
    unit_label: str = ""


@dataclass(frozen=True, slots=True)
class RecipeRunJobResult:
    """Safe browser destination after a job completes."""

    redirect_url: str
    message: str


@dataclass(frozen=True, slots=True)
class RecipeRunJob:
    """Session-scoped progress state for one Recipe run task."""

    job_id: str
    kind: RecipeRunJobKind
    project_id: str
    migration_run_id: str
    workspace_id: str
    application_id: str
    run_purpose: str
    status: RecipeRunJobStatus
    phase: RecipeRunJobPhase
    message: str
    progress_percent: int
    completed_units: int
    total_units: int
    unit_label: str
    attempt: int
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    redirect_url: str
    failure_message: str

    @property
    def active(self) -> bool:
        return self.status in {
            RecipeRunJobStatus.QUEUED,
            RecipeRunJobStatus.RUNNING,
        }

    @property
    def terminal(self) -> bool:
        return not self.active


RecipeRunProgressReporter = Callable[[RecipeRunJobProgress], None]
RecipeRunWork = Callable[[RecipeRunProgressReporter], RecipeRunJobResult]


class RecipeRunJobNotFoundError(LookupError):
    """Raised when a progress job does not belong to the requested run."""


class RecipeRunJobStateError(RuntimeError):
    """Raised when the local job supervisor is stopping."""


class RecipeRunJobManager:
    """Serialize long Recipe checks and expose thread-safe progress snapshots."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, RecipeRunJob] = {}
        self._runner = SerialJobRunner(
            worker_name="impodo-recipe-run-progress"
        )

    def enqueue(
        self,
        *,
        kind: RecipeRunJobKind,
        project_id: str,
        migration_run_id: str,
        workspace_id: str,
        application_id: str = "",
        run_purpose: str = "",
        work: RecipeRunWork,
    ) -> RecipeRunJob:
        """Create one task or return the equivalent active task."""

        with self._lock:
            active = self._active_locked(
                kind,
                migration_run_id,
                application_id,
            )
            if active is not None:
                return active
            attempt = 1 + max(
                (
                    job.attempt
                    for job in self._jobs.values()
                    if job.kind is kind
                    and job.migration_run_id == migration_run_id
                    and job.application_id == application_id
                ),
                default=0,
            )
            now = _now()
            message = (
                "Waiting to check this Odoo"
                if kind is RecipeRunJobKind.ODOO_CHECK
                else "Waiting to prepare target values"
            )
            job = RecipeRunJob(
                job_id=str(uuid4()),
                kind=kind,
                project_id=project_id,
                migration_run_id=migration_run_id,
                workspace_id=workspace_id,
                application_id=application_id,
                run_purpose=run_purpose.strip().upper(),
                status=RecipeRunJobStatus.QUEUED,
                phase=RecipeRunJobPhase.QUEUED,
                message=message,
                progress_percent=0,
                completed_units=0,
                total_units=0,
                unit_label="",
                attempt=attempt,
                created_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                redirect_url="",
                failure_message="",
            )
            self._jobs[job.job_id] = job
            try:
                self._runner.submit(
                    job.job_id,
                    lambda: self._execute(job.job_id, work),
                    discard=lambda: self._discard(job.job_id),
                )
            except SerialJobRunnerStoppedError as error:
                self._jobs.pop(job.job_id, None)
                raise RecipeRunJobStateError(
                    "Recipe run jobs are stopping"
                ) from error
            return job

    def get(
        self,
        project_id: str,
        migration_run_id: str,
        job_id: str,
    ) -> RecipeRunJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job.project_id != project_id
                or job.migration_run_id != migration_run_id
            ):
                raise RecipeRunJobNotFoundError("Recipe run job not found")
            return job

    def shutdown(self) -> None:
        """Stop accepting work without interrupting an active Odoo read."""

        self._runner.shutdown()

    def _execute(self, job_id: str, work: RecipeRunWork) -> None:
        with self._lock:
            self._mark_running(job_id)
        try:
            result = work(
                lambda progress: self._report(job_id, progress)
            )
        except Exception as error:
            with self._lock:
                self._finish_failed(job_id, _safe_failure_message(error))
        else:
            with self._lock:
                self._finish_succeeded(job_id, result)

    def _discard(self, job_id: str) -> None:
        with self._lock:
            self._finish_failed(
                job_id,
                "Impodo stopped before this queued check began.",
            )

    def _mark_running(self, job_id: str) -> RecipeRunJob:
        job = self._jobs[job_id]
        now = _now()
        updated = replace(
            job,
            status=RecipeRunJobStatus.RUNNING,
            phase=RecipeRunJobPhase.VALIDATING,
            message="Checking the saved run setup",
            progress_percent=3,
            started_at=now,
            updated_at=now,
        )
        self._jobs[job_id] = updated
        return updated

    def _report(
        self,
        job_id: str,
        progress: RecipeRunJobProgress,
    ) -> RecipeRunJob:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return job
            updated = replace(
                job,
                phase=progress.phase,
                message=progress.message.strip()[:300],
                progress_percent=max(
                    job.progress_percent,
                    min(99, max(0, int(progress.progress_percent))),
                ),
                completed_units=max(0, int(progress.completed_units)),
                total_units=max(0, int(progress.total_units)),
                unit_label=progress.unit_label.strip()[:80],
                updated_at=_now(),
            )
            self._jobs[job_id] = updated
            return updated

    def _finish_succeeded(
        self,
        job_id: str,
        result: RecipeRunJobResult,
    ) -> RecipeRunJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        updated = replace(
            job,
            status=RecipeRunJobStatus.SUCCEEDED,
            phase=RecipeRunJobPhase.COMPLETE,
            message=result.message.strip()[:300] or "Check complete",
            progress_percent=100,
            completed_units=job.total_units,
            updated_at=now,
            finished_at=now,
            redirect_url=result.redirect_url,
        )
        self._jobs[job_id] = updated
        return updated

    def _finish_failed(self, job_id: str, message: str) -> RecipeRunJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        updated = replace(
            job,
            status=RecipeRunJobStatus.FAILED,
            message="The check needs attention",
            updated_at=now,
            finished_at=now,
            failure_message=message[:1000],
        )
        self._jobs[job_id] = updated
        return updated

    def _active_locked(
        self,
        kind: RecipeRunJobKind,
        migration_run_id: str,
        application_id: str,
    ) -> RecipeRunJob | None:
        return next(
            (
                job
                for job in self._jobs.values()
                if job.active
                and job.kind is kind
                and job.migration_run_id == migration_run_id
                and job.application_id == application_id
            ),
            None,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_failure_message(error: Exception) -> str:
    """Expose expected Impodo errors without leaking implementation details."""

    if type(error).__module__.startswith("impodo."):
        message = str(error).strip()
        if message:
            return message
    return (
        "Impodo could not finish this read-only check. Return to the saved "
        "Odoo requirements and try again."
    )
