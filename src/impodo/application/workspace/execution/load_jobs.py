"""Run confirmed Odoo loads in one bounded background worker."""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from threading import Condition, RLock, Thread
from time import perf_counter
from uuid import uuid4

from impodo.application.shared.remote_wait import observe_remote_wait
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    bind_workspace_access_context,
)
from impodo.application.workspace.execution.job_models import (
    LOAD_PHASE_LABELS,
    LoadJob,
    LoadJobStatus,
    LoadPhase,
)
from impodo.domain.execution.load_control import LoadHalted
from impodo.domain.execution.models import (
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.project.foundation import MigrationIdentifierConfusionError
from impodo.domain.shared.access import Actor

from .job_projection import project_load_progress, project_load_verification
from .load_observation import (
    LoadObservation,
    bind_load_observation,
    current_load_observation,
    emit_load_evidence,
    observe_load_stage,
    record_load_failure,
)
from .progress import ExecutionProgress, execution_progress


class LoadJobNotFoundError(LookupError):
    """Raised when a load job is missing or belongs to another workspace."""


class LoadJobStateError(ValueError):
    """Raised when a requested load-job transition is unavailable."""


@dataclass(frozen=True, slots=True)
class LoadJobResult:
    """Terminal information returned by the governed load workflow."""

    execution_run_id: str
    verification_complete: bool
    completion_warning: str = ""
    completion_warning_code: str = ""


LoadProgress = Callable[[ExecutionProgress], None]
LoadVerification = Callable[[], None]
LoadWork = Callable[
    [WorkspaceAccessContext, LoadProgress, LoadVerification],
    LoadJobResult,
]


class LoadJobManager:
    """Keep non-secret control state and serialize confirmed Odoo loads."""

    def __init__(
        self,
        *,
        status_listener: Callable[[LoadJob, Actor], None] | None = None,
    ) -> None:
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._jobs: dict[str, LoadJob] = {}
        self._observations: dict[str, LoadObservation] = {}
        self._wait_started: dict[str, float] = {}
        self._pending: deque[tuple[str, Actor, LoadWork]] = deque()
        self._stopping = False
        self._worker: Thread | None = None
        self._status_listener = status_listener

    def set_status_listener(
        self,
        listener: Callable[[LoadJob, Actor], None] | None,
    ) -> None:
        """Publish coarse run progress after governed load state changes."""

        self._status_listener = listener

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        *,
        target_database: str,
        target_server: str,
        target_environment: str,
        total_rows: int,
        relationship_total_rows: int = 0,
        load_group_count: int = 0,
        recovery_run: ExecutionRun | None = None,
        access_context: WorkspaceAccessContext,
        actor: Actor,
        work: LoadWork,
    ) -> LoadJob:
        """Create one load attempt or return the workspace's active attempt."""

        if access_context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "Load access context does not belong to this workspace"
            )
        if recovery_run is not None and (
            recovery_run.workspace_id != workspace_id
            or recovery_run.status is not ExecutionRunStatus.RUNNING
        ):
            raise LoadJobStateError("The saved load does not belong to this active workspace")

        with self._condition:
            if self._stopping:
                raise LoadJobStateError("Odoo load jobs are stopping")
            active = self._active_locked(workspace_id)
            if active is not None:
                if active.access_context != access_context:
                    raise MigrationIdentifierConfusionError(
                        "Active load belongs to another workspace context"
                    )
                return active
            now = _now()
            job = LoadJob(
                job_id=str(uuid4()),
                access_context=access_context,
                workspace_id=workspace_id,
                migration_project_name=migration_project_name.strip()[:300]
                or "Data project",
                target_database=target_database.strip()[:200],
                target_server=target_server.strip()[:300],
                target_environment=target_environment.strip()[:50] or "Target",
                status=LoadJobStatus.QUEUED,
                phase=LoadPhase.QUEUED,
                message=LOAD_PHASE_LABELS[LoadPhase.QUEUED],
                total_rows=max(0, int(total_rows)),
                completed_rows=0,
                created_count=0,
                updated_count=0,
                attention_count=0,
                relationship_pending_count=0,
                relationship_total_count=max(0, int(relationship_total_rows)),
                load_group_number=0,
                load_group_count=max(0, int(load_group_count)),
                progress_percent=0,
                execution_run_id="",
                verification_complete=False,
                completion_warning="",
                completion_warning_code="",
                created_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                failure_message="",
            )
            if recovery_run is not None:
                job = replace(
                    project_load_progress(job, execution_progress(recovery_run), phase=LoadPhase.CHECKING_TARGET),
                    message="Checking saved Odoo results before resuming",
                )
            previous = self.latest(workspace_id)
            self._jobs[job.job_id] = job
            if (observer := current_load_observation()) is not None:
                observer.enqueued = True
                self._observations[job.job_id] = observer
                job = replace(job, support_reference=observer.attempt_id)
                self._jobs[job.job_id] = job
                observer.emit("enqueued", job_id=job.job_id,
                    previous_attempt_id=previous.support_reference if previous and previous.job_id != job.job_id else "",
                    entry_point="RECIPE" if access_context.recipe_application_id else "AUTHORING")
            self._pending.append((job.job_id, actor, work))
            if self._worker is None:
                self._worker = Thread(
                    target=self._work,
                    name="impodo-odoo-load",
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()
            return job

    def get(self, workspace_id: str, job_id: str) -> LoadJob:
        """Return one browser job after enforcing workspace ownership.

        Called by load-progress routes. It reads only the in-memory control
        record; callers must use the execution journal for durable outcomes.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.workspace_id != workspace_id:
                raise LoadJobNotFoundError("Odoo load job not found")
            return job

    def active(self, workspace_id: str) -> LoadJob | None:
        """Return the workspace's queued or running job for navigation/routes."""
        with self._lock:
            return self._active_locked(workspace_id)

    def latest(self, workspace_id: str) -> LoadJob | None:
        """Return the newest session job for a workspace progress page.

        Unlike ``active``, this may be terminal and therefore is unsuitable
        for deciding whether a new governed load may be started.
        """
        with self._lock:
            return max(
                (
                    job
                    for job in self._jobs.values()
                    if job.workspace_id == workspace_id
                ),
                key=lambda job: job.created_at,
                default=None,
            )

    def note_stop_requested(self, workspace_id: str, run_id: str) -> None:
        """Reflect an already durable stop while the current request finishes."""
        with self._lock:
            job = self._active_locked(workspace_id)
            if job is not None and job.execution_run_id == run_id:
                self._store(replace(job, load_control_state="STOP_REQUESTED",
                    message="Stopping after the current request", updated_at=_now()))

    def latest_many(self, workspace_ids: tuple[str, ...]) -> dict[str, LoadJob]:
        """Return one latest snapshot per requested workspace in one pass."""

        requested = set(workspace_ids)
        with self._lock:
            latest: dict[str, LoadJob] = {}
            for job in self._jobs.values():
                if job.workspace_id not in requested:
                    continue
                current = latest.get(job.workspace_id)
                if current is None or job.created_at >= current.created_at:
                    latest[job.workspace_id] = job
            return latest

    def shutdown(self) -> None:
        """Stop accepting work without interrupting an in-flight Odoo call."""

        with self._condition:
            self._stopping = True
            pending = tuple(self._pending)
            self._pending.clear()
            for job_id, _actor, _work in pending:
                self._finish_failed(
                    job_id,
                    "Impodo stopped before this queued load began. No Odoo records "
                    "were changed by this attempt.",
                )
                if (observer := self._observations.pop(job_id, None)) is not None:
                    observer.terminal = True
                    observer.emit("terminal", outcome="DISCARDED", reason="QUEUED_SHUTDOWN")
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=0.25)

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job_id, actor, work = self._pending.popleft()
                started = self._mark_running(job_id)
                access_context = self._jobs[job_id].access_context
            self._notify(started, actor)
            observer = self._observations.get(job_id)
            with bind_load_observation(observer):
                if observer is not None:
                    observer.emit("worker_started")
            with bind_load_observation(observer):
                try:
                    with (
                        bind_workspace_access_context(access_context),
                        observe_load_stage("worker"),
                        observe_remote_wait(partial(self._report_waiting, job_id)),
                    ):
                        result = work(
                            access_context,
                            partial(self._report_writing, job_id),
                            partial(self._report_verifying, job_id),
                        )
                except Exception as error:  # noqa: BLE001 - background-job boundary
                    record_load_failure(error)
                    with self._lock:
                        finished = self._finish_failed(
                            job_id,
                            _safe_failure_message(error),
                        )
                else:
                    with self._lock:
                        finished = self._finish_succeeded(job_id, result)
            if observer is not None:
                observer.terminal = True
                observer.emit("verification", verification_complete=finished.verification_complete)
                observer.emit("terminal", outcome=finished.status.value, execution_run_id=finished.execution_run_id)
                self._observations.pop(job_id, None)
            self._notify(finished, actor)

    def _mark_running(self, job_id: str) -> LoadJob:
        job = self._jobs[job_id]
        if job.status is not LoadJobStatus.QUEUED:
            raise LoadJobStateError("Odoo load is not queued")
        now = _now()
        return self._store(
            replace(
                job,
                status=LoadJobStatus.RUNNING,
                phase=LoadPhase.CHECKING_TARGET,
                message=(
                    "Checking saved Odoo results before resuming"
                    if job.execution_run_id
                    else LOAD_PHASE_LABELS[LoadPhase.CHECKING_TARGET]
                ),
                progress_percent=max(job.progress_percent, 4),
                started_at=now,
                updated_at=now,
            )
        )

    def _notify(self, job: LoadJob, actor: Actor) -> None:
        """Keep a summary failure from changing the recorded Odoo outcome."""

        if self._status_listener is None:
            return
        try:
            self._status_listener(job, actor)
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not publish Recipe load progress"
            )

    def _report_writing(self, job_id: str, progress: ExecutionProgress) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            self._store(project_load_progress(job, progress))

    def _report_waiting(self, job_id: str, seconds: float | None) -> None:
        """Expose paced waits and honor a durable stop before the next write."""
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            if seconds is None and (started := self._wait_started.pop(job_id, None)) is not None:
                emit_load_evidence(
                    "stage_finished", stage="request_wait", outcome="COMPLETED",
                    duration_ms=(perf_counter() - started) * 1000,
                )
            if job.load_control_state == "STOP_REQUESTED":
                if seconds is not None:
                    raise LoadHalted("USER_STOP")
                return
            if seconds is not None:
                self._wait_started.setdefault(job_id, perf_counter())
            message = (
                f"Waiting for Odoo's request limit; next check in {math.ceil(seconds)} seconds"
                if seconds is not None
                else LOAD_PHASE_LABELS[job.phase]
            )
            self._store(replace(job, message=message, updated_at=_now()))

    def _report_verifying(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            self._store(project_load_verification(job))

    def _finish_succeeded(
        self,
        job_id: str,
        result: LoadJobResult,
    ) -> LoadJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        message = (
            "Odoo load verified"
            if result.verification_complete
            else "Odoo load saved; verification needs review"
        )
        if job.load_control_state == "PAUSED":
            message = "Upload paused after repeated failures; review the saved results"
        elif job.load_control_state == "STOPPED":
            message = "Upload stopped with partial results"
        elif result.completion_warning_code == "LOAD_ODOO_REQUEST_DEFERRED":
            message = "Odoo load waiting; check recovery before resuming"
        return self._store(
            replace(
                job,
                status=LoadJobStatus.SUCCEEDED,
                phase=LoadPhase.COMPLETE,
                message=message,
                progress_percent=job.progress_percent if job.load_control_state == "PAUSED" else 100,
                execution_run_id=result.execution_run_id,
                verification_complete=result.verification_complete,
                completion_warning=result.completion_warning.strip()[:1000],
                completion_warning_code=(
                    result.completion_warning_code.strip()[:200]
                ),
                updated_at=now,
                finished_at=now,
            )
        )

    def _finish_failed(self, job_id: str, message: str) -> LoadJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        return self._store(
            replace(
                job,
                status=LoadJobStatus.FAILED,
                phase=LoadPhase.COMPLETE,
                message="Odoo load stopped",
                failure_message=message[:1000],
                updated_at=now,
                finished_at=now,
            )
        )

    def _active_locked(self, workspace_id: str) -> LoadJob | None:
        return max(
            (
                job
                for job in self._jobs.values()
                if job.workspace_id == workspace_id and job.active
            ),
            key=lambda job: job.created_at,
            default=None,
        )

    def _store(self, job: LoadJob) -> LoadJob:
        self._jobs[job.job_id] = job
        return job


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_failure_message(error: Exception) -> str:
    """Keep unexpected implementation details out of browser job state."""

    if type(error).__module__.startswith("impodo."):
        message = str(error).strip()
        if message:
            return message
    return (
        "Impodo stopped the load because it could not confirm the next safe "
        "action. Review the recorded totals before doing anything else."
    )
