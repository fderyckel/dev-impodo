"""Run read-only Odoo comparisons in one bounded background worker."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import Condition, RLock, Thread
from uuid import uuid4

from impodo.application.odoo_read_failures import (
    OdooReadFailure,
    classify_odoo_read_failure,
)
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    bind_workspace_access_context,
)
from impodo.domain.project.foundation import MigrationIdentifierConfusionError


class PreflightJobStatus(StrEnum):
    """Lifecycle states exposed by the browser progress endpoint."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PreflightPhase(StrEnum):
    """Coarse comparison phases that do not disclose business values."""

    QUEUED = "QUEUED"
    VERIFYING = "VERIFYING"
    READING = "READING"
    COMPARING = "COMPARING"
    BUILDING = "BUILDING"
    PUBLISHING = "PUBLISHING"
    COMPLETE = "COMPLETE"


PREFLIGHT_PHASE_LABELS: dict[PreflightPhase, str] = {
    PreflightPhase.QUEUED: "Waiting to start",
    PreflightPhase.VERIFYING: "Verifying the approved data and Odoo access",
    PreflightPhase.READING: "Reading the records needed from Odoo",
    PreflightPhase.COMPARING: "Comparing the prepared records with Odoo",
    PreflightPhase.BUILDING: "Building the final review",
    PreflightPhase.PUBLISHING: "Saving the new comparison",
    PreflightPhase.COMPLETE: "Final review ready",
}

_PHASE_PERCENT = {
    PreflightPhase.QUEUED: 0,
    PreflightPhase.VERIFYING: 5,
    PreflightPhase.READING: 20,
    PreflightPhase.COMPARING: 55,
    PreflightPhase.BUILDING: 75,
    PreflightPhase.PUBLISHING: 90,
    PreflightPhase.COMPLETE: 100,
}


@dataclass(frozen=True, slots=True)
class PreflightJobResult:
    """Safe terminal information returned by comparison orchestration."""

    preflight_run_id: str
    redirect_url: str
    completion_message: str


@dataclass(frozen=True, slots=True)
class PreflightJob:
    """One comparison attempt; credentials and compared values are excluded."""

    job_id: str
    access_context: WorkspaceAccessContext
    workspace_id: str
    migration_project_name: str
    status: PreflightJobStatus
    phase: PreflightPhase
    message: str
    progress_percent: int
    attempt: int
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    preflight_run_id: str
    redirect_url: str
    completion_message: str
    failure: OdooReadFailure | None

    @property
    def active(self) -> bool:
        return self.status in {PreflightJobStatus.QUEUED, PreflightJobStatus.RUNNING}

    @property
    def terminal(self) -> bool:
        return not self.active


PreflightProgress = Callable[[PreflightPhase], None]
PreflightWork = Callable[[PreflightProgress], PreflightJobResult]


class PreflightJobNotFoundError(LookupError):
    """Raised when a comparison job is missing or belongs elsewhere."""


class PreflightJobStateError(ValueError):
    """Raised when comparison work cannot be accepted."""


class PreflightJobManager:
    """Keep non-secret control state in memory and serialize comparisons."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._jobs: dict[str, PreflightJob] = {}
        self._pending: deque[tuple[str, PreflightWork]] = deque()
        self._stopping = False
        self._worker: Thread | None = None

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        *,
        access_context: WorkspaceAccessContext,
        work: PreflightWork,
    ) -> PreflightJob:
        """Create one attempt or return the workspace's active attempt."""

        if access_context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "Comparison access context does not belong to this workspace"
            )
        with self._condition:
            if self._stopping:
                raise PreflightJobStateError("Comparison jobs are stopping")
            active = self._active_locked(workspace_id)
            if active is not None:
                if active.access_context != access_context:
                    raise MigrationIdentifierConfusionError(
                        "Active comparison belongs to another workspace context"
                    )
                return active
            attempt = 1 + max(
                (
                    job.attempt
                    for job in self._jobs.values()
                    if job.workspace_id == workspace_id
                ),
                default=0,
            )
            now = _now()
            job = PreflightJob(
                job_id=str(uuid4()),
                access_context=access_context,
                workspace_id=workspace_id,
                migration_project_name=(
                    migration_project_name.strip()[:300] or "Data project"
                ),
                status=PreflightJobStatus.QUEUED,
                phase=PreflightPhase.QUEUED,
                message=PREFLIGHT_PHASE_LABELS[PreflightPhase.QUEUED],
                progress_percent=0,
                attempt=attempt,
                created_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                preflight_run_id="",
                redirect_url="",
                completion_message="",
                failure=None,
            )
            self._jobs[job.job_id] = job
            self._pending.append((job.job_id, work))
            if self._worker is None:
                self._worker = Thread(
                    target=self._work,
                    name="impodo-preflight",
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()
            return job

    def get(self, workspace_id: str, job_id: str) -> PreflightJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.workspace_id != workspace_id:
                raise PreflightJobNotFoundError("Comparison job not found")
            return job

    def active(self, workspace_id: str) -> PreflightJob | None:
        with self._lock:
            return self._active_locked(workspace_id)

    def shutdown(self) -> None:
        """Stop accepting work without interrupting an in-flight Odoo read."""

        with self._condition:
            self._stopping = True
            pending_ids = tuple(job_id for job_id, _work in self._pending)
            self._pending.clear()
            for job_id in pending_ids:
                self._finish_failed(
                    job_id,
                    RuntimeError("Impodo stopped before the comparison started"),
                )
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
                job_id, work = self._pending.popleft()
                self._mark_running(job_id)
                access_context = self._jobs[job_id].access_context
            try:
                with bind_workspace_access_context(access_context):
                    result = work(lambda phase: self._update_phase(job_id, phase))
            except Exception as error:
                with self._lock:
                    self._finish_failed(job_id, error)
            else:
                with self._lock:
                    self._finish_succeeded(job_id, result)

    def _mark_running(self, job_id: str) -> None:
        now = _now()
        job = self._jobs[job_id]
        self._store(
            replace(
                job,
                status=PreflightJobStatus.RUNNING,
                phase=PreflightPhase.VERIFYING,
                message=PREFLIGHT_PHASE_LABELS[PreflightPhase.VERIFYING],
                progress_percent=_PHASE_PERCENT[PreflightPhase.VERIFYING],
                started_at=now,
                updated_at=now,
            )
        )

    def _update_phase(self, job_id: str, phase: PreflightPhase) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            if _PHASE_PERCENT[phase] < job.progress_percent:
                return
            self._store(
                replace(
                    job,
                    phase=phase,
                    message=PREFLIGHT_PHASE_LABELS[phase],
                    progress_percent=_PHASE_PERCENT[phase],
                    updated_at=_now(),
                )
            )

    def _finish_succeeded(
        self,
        job_id: str,
        result: PreflightJobResult,
    ) -> PreflightJob:
        now = _now()
        job = self._jobs[job_id]
        return self._store(
            replace(
                job,
                status=PreflightJobStatus.SUCCEEDED,
                phase=PreflightPhase.COMPLETE,
                message=PREFLIGHT_PHASE_LABELS[PreflightPhase.COMPLETE],
                progress_percent=100,
                preflight_run_id=result.preflight_run_id,
                redirect_url=result.redirect_url,
                completion_message=result.completion_message,
                updated_at=now,
                finished_at=now,
            )
        )

    def _finish_failed(self, job_id: str, error: Exception) -> PreflightJob:
        now = _now()
        job = self._jobs[job_id]
        return self._store(
            replace(
                job,
                status=PreflightJobStatus.FAILED,
                phase=PreflightPhase.COMPLETE,
                message="Comparison needs attention",
                failure=classify_odoo_read_failure(error),
                updated_at=now,
                finished_at=now,
            )
        )

    def _active_locked(self, workspace_id: str) -> PreflightJob | None:
        return max(
            (
                job
                for job in self._jobs.values()
                if job.workspace_id == workspace_id and job.active
            ),
            key=lambda job: job.created_at,
            default=None,
        )

    def _store(self, job: PreflightJob) -> PreflightJob:
        self._jobs[job.job_id] = job
        return job


def _now() -> datetime:
    return datetime.now(timezone.utc)
