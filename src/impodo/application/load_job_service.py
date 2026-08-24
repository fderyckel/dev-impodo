"""Run confirmed Odoo loads in one bounded background worker."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Condition, RLock, Thread
from uuid import uuid4

from ..domain.execution import (
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from ..load_jobs import (
    LOAD_PHASE_LABELS,
    LoadJob,
    LoadJobStatus,
    LoadPhase,
)
from ..migration_foundation import MigrationIdentifierConfusionError
from ..workspace_access import (
    WorkspaceAccessContext,
    bind_workspace_access_context,
)


class LoadJobNotFoundError(LookupError):
    """Raised when a load job is missing or belongs to another workspace."""


class LoadJobStateError(ValueError):
    """Raised when a requested load-job transition is unavailable."""


@dataclass(frozen=True, slots=True)
class LoadJobResult:
    """Terminal information returned by the governed load workflow."""

    execution_run_id: str
    verification_complete: bool


LoadProgress = Callable[[ExecutionRun], None]
LoadWork = Callable[
    [WorkspaceAccessContext, LoadProgress, LoadProgress],
    LoadJobResult,
]


class LoadJobManager:
    """Keep non-secret control state and serialize confirmed Odoo loads."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._jobs: dict[str, LoadJob] = {}
        self._pending: deque[tuple[str, LoadWork]] = deque()
        self._stopping = False
        self._worker: Thread | None = None

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        *,
        target_database: str,
        target_server: str,
        target_environment: str,
        total_rows: int,
        access_context: WorkspaceAccessContext,
        work: LoadWork,
    ) -> LoadJob:
        """Create one load attempt or return the workspace's active attempt."""

        if access_context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "Load access context does not belong to this workspace"
            )

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
                migration_project_name=migration_project_name.strip()[:300] or "Data project",
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
                progress_percent=0,
                execution_run_id="",
                verification_complete=False,
                created_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                failure_message="",
            )
            self._jobs[job.job_id] = job
            self._pending.append((job.job_id, work))
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
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.workspace_id != workspace_id:
                raise LoadJobNotFoundError("Odoo load job not found")
            return job

    def active(self, workspace_id: str) -> LoadJob | None:
        with self._lock:
            return self._active_locked(workspace_id)

    def latest(self, workspace_id: str) -> LoadJob | None:
        with self._lock:
            return max(
                (job for job in self._jobs.values() if job.workspace_id == workspace_id),
                key=lambda job: job.created_at,
                default=None,
            )

    def shutdown(self) -> None:
        """Stop accepting work without interrupting an in-flight Odoo call."""

        with self._condition:
            self._stopping = True
            pending = tuple(self._pending)
            self._pending.clear()
            for job_id, _work in pending:
                self._finish_failed(
                    job_id,
                    "Impodo stopped before this queued load began. No Odoo records "
                    "were changed by this attempt.",
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
                    result = work(
                        access_context,
                        lambda run: self._report_writing(job_id, run),
                        lambda run: self._report_verifying(job_id, run),
                    )
            except Exception as error:
                with self._lock:
                    self._finish_failed(job_id, _safe_failure_message(error))
            else:
                with self._lock:
                    self._finish_succeeded(job_id, result)

    def _mark_running(self, job_id: str) -> None:
        job = self._jobs[job_id]
        if job.status is not LoadJobStatus.QUEUED:
            raise LoadJobStateError("Odoo load is not queued")
        now = _now()
        self._store(
            replace(
                job,
                status=LoadJobStatus.RUNNING,
                phase=LoadPhase.CHECKING_TARGET,
                message=LOAD_PHASE_LABELS[LoadPhase.CHECKING_TARGET],
                progress_percent=4,
                started_at=now,
                updated_at=now,
            )
        )

    def _report_writing(self, job_id: str, run: ExecutionRun) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            self._store(_job_from_run(job, run, phase=LoadPhase.WRITING))

    def _report_verifying(self, job_id: str, run: ExecutionRun) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            updated = _job_from_run(job, run, phase=LoadPhase.VERIFYING)
            self._store(replace(updated, progress_percent=max(90, updated.progress_percent)))

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
        return self._store(
            replace(
                job,
                status=LoadJobStatus.SUCCEEDED,
                phase=LoadPhase.COMPLETE,
                message=message,
                progress_percent=100,
                execution_run_id=result.execution_run_id,
                verification_complete=result.verification_complete,
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


def _job_from_run(job: LoadJob, run: ExecutionRun, *, phase: LoadPhase) -> LoadJob:
    """Project durable journal states into non-sensitive browser counters."""

    committed = {
        ExecutionRowStatus.COMMITTED,
        ExecutionRowStatus.PARTIALLY_APPLIED,
    }
    completed_rows = run.total_count - run.planned_count
    created_count = sum(
        row.operation == "CREATE" and row.status in committed for row in run.rows
    )
    updated_count = sum(
        row.operation == "UPDATE"
        and row.status is ExecutionRowStatus.COMMITTED
        for row in run.rows
    )
    relationship_pending_count = sum(
        row.status is ExecutionRowStatus.PARTIALLY_APPLIED for row in run.rows
    )
    attention_statuses = {
        ExecutionRowStatus.FAILED,
        ExecutionRowStatus.BLOCKED,
        ExecutionRowStatus.OUTCOME_UNKNOWN,
    }
    attention_count = sum(row.status in attention_statuses for row in run.rows)
    if run.status is not ExecutionRunStatus.RUNNING:
        attention_count += relationship_pending_count
    fraction = completed_rows / run.total_count if run.total_count else 0.0
    percent = min(82, 8 + round(74 * max(0.0, min(1.0, fraction))))
    return replace(
        job,
        phase=phase,
        message=LOAD_PHASE_LABELS[phase],
        total_rows=run.total_count,
        completed_rows=completed_rows,
        created_count=created_count,
        updated_count=updated_count,
        attention_count=attention_count,
        relationship_pending_count=relationship_pending_count,
        progress_percent=max(job.progress_percent, percent),
        execution_run_id=run.run_id,
        updated_at=_now(),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
