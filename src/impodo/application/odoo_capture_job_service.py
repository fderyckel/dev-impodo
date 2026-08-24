"""Run browser Odoo captures in one bounded background worker."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from threading import Condition, Event, RLock, Thread
from uuid import uuid4

from ..access import Actor
from ..domain.odoo_source_capture import OdooSourceCaptureCancelled
from ..odoo_capture_jobs import (
    CAPTURE_PHASE_LABELS,
    OdooCaptureJob,
    OdooCaptureJobStatus,
    OdooCapturePhase,
    OdooCaptureProgress,
    odoo_capture_progress_percent,
)
from ..migration_foundation import MigrationIdentifierConfusionError
from ..workspace_access import (
    WorkspaceAccessContext,
    bind_workspace_access_context,
)
from .odoo_capture_publication_service import (
    OdooCapturePublication,
    OdooCapturePublicationService,
)
from .odoo_source_capture_service import OdooSourceCapturePort


class OdooCaptureJobNotFoundError(LookupError):
    """Raised when a job is missing or belongs to another workspace."""


class OdooCaptureJobStateError(ValueError):
    """Raised when a requested job transition is not available."""


class OdooCaptureJobManager:
    """Keep control-plane state in memory and serialize heavy capture work."""

    def __init__(
        self,
        publication: OdooCapturePublicationService,
        *,
        accept_publication: Callable[
            [str, OdooCapturePublication, Actor], None
        ]
        | None = None,
    ) -> None:
        self._publication = publication
        self._accept_publication = accept_publication
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._jobs: dict[str, OdooCaptureJob] = {}
        self._pending: deque[tuple[str, OdooSourceCapturePort, Actor]] = deque()
        self._cancellations: dict[str, Event] = {}
        self._stopping = False
        self._worker: Thread | None = None

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        maximum_rows: int,
        gateway: OdooSourceCapturePort,
        *,
        access_context: WorkspaceAccessContext,
        actor: Actor,
    ) -> OdooCaptureJob:
        """Create one attempt or return the workspace's active attempt."""

        if access_context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "Capture access context does not belong to this workspace"
            )

        with self._condition:
            if self._stopping:
                raise OdooCaptureJobStateError("Odoo capture jobs are stopping")
            active = self._active_locked(workspace_id)
            if active is not None:
                if active.access_context != access_context:
                    raise MigrationIdentifierConfusionError(
                        "Active capture belongs to another workspace context"
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
            job = OdooCaptureJob(
                job_id=str(uuid4()),
                access_context=access_context,
                workspace_id=workspace_id,
                migration_project_name=migration_project_name.strip()[:300] or "Odoo source project",
                status=OdooCaptureJobStatus.QUEUED,
                phase=OdooCapturePhase.QUEUED,
                message=CAPTURE_PHASE_LABELS[OdooCapturePhase.QUEUED],
                completed_rows=0,
                total_rows=max(0, int(maximum_rows)),
                page_count=0,
                response_bytes=0,
                normalized_bytes=0,
                progress_percent=0,
                attempt=attempt,
                cancel_requested=False,
                created_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                manifest_id="",
                failure_message="",
            )
            self._jobs[job.job_id] = job
            self._cancellations[job.job_id] = Event()
            self._pending.append((job.job_id, gateway, actor))
            if self._worker is None:
                self._worker = Thread(
                    target=self._work,
                    name="impodo-odoo-capture",
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()
            return job

    def get(self, workspace_id: str, job_id: str) -> OdooCaptureJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.workspace_id != workspace_id:
                raise OdooCaptureJobNotFoundError("Odoo capture job not found")
            return job

    def active(self, workspace_id: str) -> OdooCaptureJob | None:
        with self._lock:
            return self._active_locked(workspace_id)

    def cancel(self, workspace_id: str, job_id: str) -> OdooCaptureJob:
        with self._condition:
            job = self.get(workspace_id, job_id)
            if job.terminal:
                return job
            self._cancellations[job_id].set()
            job = self._store(
                replace(
                    job,
                    cancel_requested=True,
                    message="Stopping safely after the current Odoo page",
                    updated_at=_now(),
                )
            )
            for pending in tuple(self._pending):
                if pending[0] == job_id:
                    self._pending.remove(pending)
                    return self._finish_cancelled(job_id)
            return job

    def shutdown(self) -> None:
        """Cancel session work without waiting on a network timeout."""

        with self._condition:
            self._stopping = True
            for cancellation in self._cancellations.values():
                cancellation.set()
            pending_ids = tuple(item[0] for item in self._pending)
            self._pending.clear()
            for job_id in pending_ids:
                self._finish_cancelled(job_id)
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
                job_id, gateway, actor = self._pending.popleft()
                cancellation = self._cancellations[job_id]
                self._mark_running(job_id)
                access_context = self._jobs[job_id].access_context
            try:
                with bind_workspace_access_context(access_context):
                    publication = self._publication.publish(
                        access_context.workspace_id,
                        gateway,
                        actor=actor,
                        cancellation=cancellation.is_set,
                        progress=lambda value: self._update_progress(job_id, value),
                    )
                    if self._accept_publication is not None:
                        self._accept_publication(
                            access_context.workspace_id,
                            publication,
                            actor,
                        )
            except OdooSourceCaptureCancelled:
                with self._lock:
                    self._finish_cancelled(job_id)
            except Exception as error:
                with self._lock:
                    self._finish_failed(job_id, _safe_failure_message(error))
            else:
                with self._lock:
                    self._finish_succeeded(job_id, publication.manifest.manifest_id)
            finally:
                with self._lock:
                    self._cancellations.pop(job_id, None)

    def _mark_running(self, job_id: str) -> None:
        job = self._jobs[job_id]
        if job.status is not OdooCaptureJobStatus.QUEUED:
            raise OdooCaptureJobStateError("Odoo capture is not queued")
        now = _now()
        self._store(
            replace(
                job,
                status=OdooCaptureJobStatus.RUNNING,
                phase=OdooCapturePhase.VERIFYING,
                message=CAPTURE_PHASE_LABELS[OdooCapturePhase.VERIFYING],
                progress_percent=5,
                started_at=now,
                updated_at=now,
            )
        )

    def _update_progress(
        self,
        job_id: str,
        progress: OdooCaptureProgress,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            self._store(
                replace(
                    job,
                    phase=progress.phase,
                    message=(
                        job.message
                        if job.cancel_requested
                        else CAPTURE_PHASE_LABELS[progress.phase]
                    ),
                    completed_rows=max(job.completed_rows, progress.completed_rows),
                    total_rows=max(job.total_rows, progress.total_rows),
                    page_count=max(job.page_count, progress.page_count),
                    response_bytes=max(job.response_bytes, progress.response_bytes),
                    normalized_bytes=max(
                        job.normalized_bytes,
                        progress.normalized_bytes,
                    ),
                    progress_percent=max(
                        job.progress_percent,
                        odoo_capture_progress_percent(progress),
                    ),
                    updated_at=_now(),
                )
            )

    def _finish_succeeded(self, job_id: str, manifest_id: str) -> OdooCaptureJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        return self._store(
            replace(
                job,
                status=OdooCaptureJobStatus.SUCCEEDED,
                phase=OdooCapturePhase.COMPLETE,
                message=CAPTURE_PHASE_LABELS[OdooCapturePhase.COMPLETE],
                progress_percent=100,
                manifest_id=manifest_id,
                updated_at=now,
                finished_at=now,
            )
        )

    def _finish_cancelled(self, job_id: str) -> OdooCaptureJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        return self._store(
            replace(
                job,
                status=OdooCaptureJobStatus.CANCELLED,
                phase=OdooCapturePhase.COMPLETE,
                message="Odoo capture stopped safely",
                cancel_requested=True,
                updated_at=now,
                finished_at=now,
            )
        )

    def _finish_failed(self, job_id: str, message: str) -> OdooCaptureJob:
        job = self._jobs[job_id]
        if job.terminal:
            return job
        now = _now()
        return self._store(
            replace(
                job,
                status=OdooCaptureJobStatus.FAILED,
                phase=OdooCapturePhase.COMPLETE,
                message="Odoo capture could not be completed",
                failure_message=message[:1000],
                updated_at=now,
                finished_at=now,
            )
        )

    def _active_locked(self, workspace_id: str) -> OdooCaptureJob | None:
        return max(
            (
                job
                for job in self._jobs.values()
                if job.workspace_id == workspace_id and job.active
            ),
            key=lambda job: job.created_at,
            default=None,
        )

    def _store(self, job: OdooCaptureJob) -> OdooCaptureJob:
        self._jobs[job.job_id] = job
        return job


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_failure_message(error: Exception) -> str:
    """Keep unexpected implementation detail out of browser job state."""

    if type(error).__module__.startswith("impodo."):
        message = str(error).strip()
        if message:
            return message
    return (
        "Impodo stopped before publishing a new frozen version. "
        "The previous current version is unchanged; review the Odoo connection "
        "and try again."
    )
