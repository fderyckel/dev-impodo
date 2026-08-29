"""Run completed-load correction review and apply work off the HTTP request."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import Condition, RLock, Thread
from uuid import uuid4

from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.correction import CorrectionPlanError
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.execution.odoo_readback import OdooReadbackError
from impodo.domain.execution.odoo_write import OdooWriteError
from impodo.domain.shared.access import AuthorizationError
from impodo.domain.workspace.workbench import WorkspaceStateError


class CorrectionJobKind(StrEnum):
    REVIEW = "REVIEW"
    APPLY = "APPLY"


class CorrectionJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CorrectionJobResult:
    """Safe terminal counts; protected field values and IDs never enter it."""

    field_count: int = 0
    record_count: int = 0
    already_corrected_count: int = 0
    blocker_messages: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True, slots=True)
class CorrectionJob:
    job_id: str
    completed_workspace_id: str
    successor_workspace_id: str
    kind: CorrectionJobKind
    status: CorrectionJobStatus
    message: str
    progress_percent: int
    result: CorrectionJobResult | None
    failure_message: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {
            CorrectionJobStatus.SUCCEEDED,
            CorrectionJobStatus.FAILED,
        }


CorrectionWork = Callable[
    [Callable[[int, str], None]],
    CorrectionJobResult,
]


class CorrectionJobManager:
    """Serialize sparse correction jobs and retain progress across page reloads."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._jobs: dict[str, CorrectionJob] = {}
        self._pending: deque[tuple[str, CorrectionWork]] = deque()
        self._worker: Thread | None = None
        self._stopping = False

    def enqueue(
        self,
        completed_workspace_id: str,
        successor_workspace_id: str,
        *,
        kind: CorrectionJobKind,
        work: CorrectionWork,
    ) -> CorrectionJob:
        with self._condition:
            if self._stopping:
                raise ValueError("Correction jobs are stopping")
            active = self._active_locked(completed_workspace_id)
            if active is not None:
                return active
            now = _now()
            job = CorrectionJob(
                job_id=str(uuid4()),
                completed_workspace_id=completed_workspace_id,
                successor_workspace_id=successor_workspace_id,
                kind=CorrectionJobKind(kind),
                status=CorrectionJobStatus.QUEUED,
                message="Correction review queued"
                if kind is CorrectionJobKind.REVIEW
                else "Correction apply queued",
                progress_percent=0,
                result=None,
                failure_message="",
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.job_id] = job
            self._pending.append((job.job_id, work))
            if self._worker is None:
                self._worker = Thread(
                    target=self._run,
                    name="impodo-correction",
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()
            return job

    def get(self, completed_workspace_id: str, job_id: str) -> CorrectionJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job.completed_workspace_id != completed_workspace_id
            ):
                raise LookupError("Correction job not found")
            return job

    def active(self, completed_workspace_id: str) -> CorrectionJob | None:
        with self._lock:
            return self._active_locked(completed_workspace_id)

    def latest(self, completed_workspace_id: str) -> CorrectionJob | None:
        with self._lock:
            return max(
                (
                    item
                    for item in self._jobs.values()
                    if item.completed_workspace_id == completed_workspace_id
                ),
                key=lambda item: item.created_at,
                default=None,
            )

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            while self._pending:
                job_id, _work = self._pending.popleft()
                self._store_failure(
                    job_id,
                    "Impodo stopped before this correction began.",
                )
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=0.25)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job_id, work = self._pending.popleft()
                job = self._jobs[job_id]
                self._jobs[job_id] = replace(
                    job,
                    status=CorrectionJobStatus.RUNNING,
                    message="Checking current correction evidence",
                    progress_percent=5,
                    updated_at=_now(),
                )
            try:
                result = work(
                    lambda percent, message: self._progress(
                        job_id,
                        percent,
                        message,
                    )
                )
            except Exception as error:
                with self._lock:
                    self._store_failure(job_id, _safe_message(error))
            else:
                with self._lock:
                    job = self._jobs[job_id]
                    now = _now()
                    self._jobs[job_id] = replace(
                        job,
                        status=CorrectionJobStatus.SUCCEEDED,
                        message=(
                            "Correction review ready"
                            if job.kind is CorrectionJobKind.REVIEW
                            else (
                                "Correction verified"
                                if result.verified
                                else "Correction outcome needs attention"
                            )
                        ),
                        progress_percent=100,
                        result=result,
                        updated_at=now,
                        finished_at=now,
                    )

    def _progress(self, job_id: str, percent: int, message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            self._jobs[job_id] = replace(
                job,
                progress_percent=max(
                    job.progress_percent,
                    min(99, max(1, int(percent))),
                ),
                message=message.strip()[:300] or job.message,
                updated_at=_now(),
            )

    def _store_failure(self, job_id: str, message: str) -> None:
        job = self._jobs[job_id]
        now = _now()
        self._jobs[job_id] = replace(
            job,
            status=CorrectionJobStatus.FAILED,
            message="Correction stopped safely",
            failure_message=message,
            updated_at=now,
            finished_at=now,
        )

    def _active_locked(self, completed_workspace_id: str) -> CorrectionJob | None:
        return next(
            (
                item
                for item in self._jobs.values()
                if item.completed_workspace_id == completed_workspace_id
                and not item.terminal
            ),
            None,
        )


def _safe_message(error: Exception) -> str:
    if not isinstance(
        error,
        (
            AuthorizationError,
            CorrectionOriginError,
            CorrectionPlanError,
            OdooReadbackError,
            OdooWriteError,
            SecretStoreError,
            WorkspaceStateError,
        ),
    ):
        return "Correction stopped safely. Try again or review the current evidence."
    message = str(error).strip()
    return message[:500] if message else "Correction stopped safely. Try again."


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "CorrectionJob",
    "CorrectionJobKind",
    "CorrectionJobManager",
    "CorrectionJobResult",
    "CorrectionJobStatus",
]
