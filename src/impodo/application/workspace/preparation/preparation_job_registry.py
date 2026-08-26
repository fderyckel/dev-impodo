"""Thread-safe, session-only state for background preparation jobs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from impodo.domain.shared.access import ActorIdentity
from impodo.application.shared.build_contract import ApplicationBuildContract
from impodo.application.workspace.preparation.job_models import (
    PHASE_LABELS,
    PreparationJob,
    PreparationJobStatus,
    PreparationPhase,
    PreparationWorkspace,
    preparation_progress_percent,
)


class PreparationJobNotFoundError(LookupError):
    """Raised when a job ID is absent or belongs to another workspace."""


class PreparationJobStateError(ValueError):
    """Raised when a requested job transition is not allowed."""


class PreparationJobRegistry:
    """Keep small progress snapshots in memory for this application session."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, PreparationJob] = {}

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        total_rows: int,
        requested_by: ActorIdentity,
        workspace: PreparationWorkspace,
        build_contract: ApplicationBuildContract,
    ) -> tuple[PreparationJob, bool]:
        """Create one attempt or return the workspace's already-active attempt."""

        with self._lock:
            active = self._active_locked(workspace_id)
            if active is not None:
                return active, False
            attempt = 1 + max(
                (
                    job.attempt
                    for job in self._jobs.values()
                    if job.workspace_id == workspace_id
                ),
                default=0,
            )
            now = _now()
            job = PreparationJob(
                job_id=str(uuid4()),
                workspace_id=workspace_id,
                migration_project_name=migration_project_name.strip()[:300] or "Data preparation project",
                build_contract=build_contract,
                workspace=workspace,
                status=PreparationJobStatus.QUEUED,
                phase=PreparationPhase.QUEUED,
                message=PHASE_LABELS[PreparationPhase.QUEUED],
                completed_rows=0,
                total_rows=max(0, int(total_rows)),
                progress_percent=0,
                attempt=attempt,
                cancel_requested=False,
                requested_by_issuer=requested_by.issuer,
                requested_by_subject=requested_by.subject_id,
                requested_by_display_name=requested_by.display_name,
                created_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                result_run_id="",
                failure_code="",
                failure_message="",
            )
            self._jobs[job.job_id] = job
            return job, True

    def get(self, workspace_id: str, job_id: str) -> PreparationJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.workspace_id != workspace_id:
                raise PreparationJobNotFoundError("Preparation job not found")
            return job

    def get_by_id(self, job_id: str) -> PreparationJob:
        with self._lock:
            return self._get_by_id_locked(job_id)

    def active(self, workspace_id: str) -> PreparationJob | None:
        with self._lock:
            return self._active_locked(workspace_id)

    def latest_many(
        self,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, PreparationJob]:
        """Return one latest snapshot per requested workspace in one pass."""

        requested = set(workspace_ids)
        with self._lock:
            latest: dict[str, PreparationJob] = {}
            for job in self._jobs.values():
                if job.workspace_id not in requested:
                    continue
                current = latest.get(job.workspace_id)
                if current is None or (
                    job.created_at,
                    job.attempt,
                ) > (
                    current.created_at,
                    current.attempt,
                ):
                    latest[job.workspace_id] = job
            return latest

    def delete_workspace_history(self, workspace_id: str) -> None:
        with self._lock:
            if self._active_locked(workspace_id) is not None:
                raise PreparationJobStateError(
                    "Stop the active preparation before deleting this workspace"
                )
            self._jobs = {
                job_id: job
                for job_id, job in self._jobs.items()
                if job.workspace_id != workspace_id
            }

    def mark_running(self, job_id: str) -> PreparationJob:
        with self._lock:
            job = self._get_by_id_locked(job_id)
            if job.status is PreparationJobStatus.RUNNING:
                return job
            if job.status is not PreparationJobStatus.QUEUED:
                raise PreparationJobStateError(
                    "Preparation job transition is not allowed"
                )
            now = _now()
            return self._store(
                replace(
                    job,
                    status=PreparationJobStatus.RUNNING,
                    phase=PreparationPhase.VALIDATING,
                    message=PHASE_LABELS[PreparationPhase.VALIDATING],
                    progress_percent=3,
                    started_at=job.started_at or now,
                    updated_at=now,
                )
            )

    def update_progress(
        self,
        job_id: str,
        phase: PreparationPhase,
        *,
        completed_rows: int,
        total_rows: int,
        message: str = "",
    ) -> PreparationJob:
        with self._lock:
            job = self._get_by_id_locked(job_id)
            if job.terminal:
                return job
            if job.status is not PreparationJobStatus.RUNNING:
                raise PreparationJobStateError("Preparation job is not running")
            completed = max(job.completed_rows, max(0, int(completed_rows)))
            total = max(job.total_rows, max(0, int(total_rows)))
            percent = max(
                job.progress_percent,
                preparation_progress_percent(
                    phase,
                    completed_rows=completed,
                    total_rows=total,
                ),
            )
            return self._store(
                replace(
                    job,
                    phase=phase,
                    message=message.strip()[:500] or PHASE_LABELS[phase],
                    completed_rows=completed,
                    total_rows=total,
                    progress_percent=percent,
                    updated_at=_now(),
                )
            )

    def request_cancel(self, workspace_id: str, job_id: str) -> PreparationJob:
        with self._lock:
            job = self.get(workspace_id, job_id)
            if job.terminal:
                return job
            if not job.active:
                raise PreparationJobStateError("Preparation job cannot be cancelled")
            return self._store(
                replace(
                    job,
                    cancel_requested=True,
                    message="Stopping safely after the current batch",
                    updated_at=_now(),
                )
            )

    def mark_succeeded(self, job_id: str, result_run_id: str) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.SUCCEEDED,
            message="Prepared data is ready for review",
            result_run_id=result_run_id[:200],
        )

    def mark_review_required(self, job_id: str) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.REVIEW_REQUIRED,
            message="Possible duplicate records need your review",
        )

    def mark_cancelled(self, job_id: str) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.CANCELLED,
            message="Preparation was stopped safely",
            cancel_requested=True,
        )

    def mark_failed(
        self,
        job_id: str,
        failure_code: str,
        failure_message: str,
    ) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.FAILED,
            message="Preparation could not be completed",
            failure_code=failure_code.strip()[:200],
            failure_message=failure_message.strip()[:1000],
        )

    def _finish(
        self,
        job_id: str,
        *,
        status: PreparationJobStatus,
        message: str,
        result_run_id: str = "",
        failure_code: str = "",
        failure_message: str = "",
        cancel_requested: bool | None = None,
    ) -> PreparationJob:
        with self._lock:
            job = self._get_by_id_locked(job_id)
            if job.terminal:
                if job.status is status:
                    return job
                raise PreparationJobStateError("Preparation job is already complete")
            now = _now()
            return self._store(
                replace(
                    job,
                    status=status,
                    phase=PreparationPhase.COMPLETE,
                    message=message[:500],
                    progress_percent=(
                        100
                        if status
                        in {
                            PreparationJobStatus.SUCCEEDED,
                            PreparationJobStatus.REVIEW_REQUIRED,
                        }
                        else job.progress_percent
                    ),
                    cancel_requested=(
                        job.cancel_requested
                        if cancel_requested is None
                        else cancel_requested
                    ),
                    result_run_id=result_run_id,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    finished_at=now,
                    updated_at=now,
                )
            )

    def _active_locked(self, workspace_id: str) -> PreparationJob | None:
        active = (
            job
            for job in self._jobs.values()
            if job.workspace_id == workspace_id and job.active
        )
        return max(active, key=lambda job: job.created_at, default=None)

    def _get_by_id_locked(self, job_id: str) -> PreparationJob:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise PreparationJobNotFoundError("Preparation job not found") from error

    def _store(self, job: PreparationJob) -> PreparationJob:
        self._jobs[job.job_id] = job
        return job


def _now() -> datetime:
    return datetime.now(timezone.utc)
