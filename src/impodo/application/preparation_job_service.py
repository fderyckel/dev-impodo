"""Run heavy preparation in a child process and supervise live progress."""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing.context import BaseContext
from pathlib import Path
from queue import Empty
from threading import RLock, Thread
from typing import Any

from ..access import Actor
from .preparation_job_registry import (
    PreparationJobRegistry,
    PreparationJobStateError,
)
from ..connectors import ConnectorError
from ..domain.errors import ReadinessError
from ..preparation_jobs import PreparationJob, PreparationJobStatus, PreparationPhase
from ..projects import ProjectError
from ..secrets import SecretStoreError
from ..workspace_errors import WorkspaceError


class PreparationCancelled(RuntimeError):
    """Stop source ingestion at a safe batch boundary."""


@dataclass(slots=True)
class _RunningWorker:
    process: Any
    events: Any
    cancel: Any
    supervisor: Thread


class PreparationJobManager:
    """Enqueue, supervise, cancel, and inspect local preparation processes."""

    def __init__(
        self,
        root: str | Path,
        *,
        process_context: BaseContext | None = None,
        max_workers: int = 1,
    ) -> None:
        import multiprocessing

        self.root = Path(root).resolve()
        self.registry = PreparationJobRegistry()
        self._process_context = process_context or multiprocessing.get_context("spawn")
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        self._max_workers = max_workers
        self._lock = RLock()
        self._workers: dict[str, _RunningWorker] = {}
        self._pending: dict[str, tuple[PreparationJob, Actor]] = {}

    def enqueue(
        self,
        project_id: str,
        project_name: str,
        total_rows: int,
        *,
        actor: Actor,
    ) -> PreparationJob:
        """Register one attempt and start it without holding the HTTP request."""

        job, created = self.registry.enqueue(
            project_id,
            project_name,
            total_rows,
            actor.identity,
        )
        if created:
            with self._lock:
                self._pending[job.job_id] = (job, actor)
                self._schedule_locked()
        return job

    def retry(
        self,
        project_id: str,
        job_id: str,
        project_name: str,
        total_rows: int,
        *,
        actor: Actor,
    ) -> PreparationJob:
        """Create a fresh attempt after a failed or cancelled job."""

        previous = self.registry.get(project_id, job_id)
        if previous.status not in {
            PreparationJobStatus.FAILED,
            PreparationJobStatus.CANCELLED,
        }:
            raise PreparationJobStateError(
                "Only a failed or stopped preparation can be tried again"
            )
        return self.enqueue(
            project_id,
            project_name,
            total_rows,
            actor=actor,
        )

    def get(self, project_id: str, job_id: str) -> PreparationJob:
        return self.registry.get(project_id, job_id)

    def active(self, project_id: str) -> PreparationJob | None:
        return self.registry.active(project_id)

    def delete_project_history(self, project_id: str) -> None:
        self.registry.delete_project_history(project_id)

    def cancel(self, project_id: str, job_id: str) -> PreparationJob:
        job = self.registry.request_cancel(project_id, job_id)
        with self._lock:
            worker = self._workers.get(job_id)
            if worker is not None:
                worker.cancel.set()
                return job
            if self._pending.pop(job_id, None) is not None:
                stopped = self.registry.mark_cancelled(job_id)
                self._schedule_locked()
                return stopped
        return job

    def worker_alive(self, job_id: str) -> bool:
        """Return whether this app instance still owns a live worker process."""

        with self._lock:
            worker = self._workers.get(job_id)
            return bool(worker is not None and worker.process.is_alive())

    def worker_pid(self, job_id: str) -> int | None:
        """Return the live child PID for local resource diagnostics."""

        with self._lock:
            worker = self._workers.get(job_id)
            if worker is None or not worker.process.is_alive():
                return None
            return int(worker.process.pid)

    def shutdown(self) -> None:
        """Request safe stops; daemon workers are reclaimed with the app process."""

        with self._lock:
            workers = tuple(self._workers.values())
            pending_ids = tuple(self._pending)
            self._pending.clear()
        for job_id in pending_ids:
            try:
                self.registry.mark_cancelled(job_id)
            except PreparationJobStateError:
                pass
        for worker in workers:
            worker.cancel.set()
        for worker in workers:
            worker.supervisor.join(timeout=0.25)

    def _schedule_locked(self) -> None:
        """Start queued attempts up to the configured local RAM guardrail."""

        while self._pending and len(self._workers) < self._max_workers:
            job_id = next(iter(self._pending))
            job, actor = self._pending.pop(job_id)
            try:
                self._start(job, actor)
            except Exception:
                # _start records the actionable terminal failure. Continue so
                # one launch problem cannot strand unrelated queued projects.
                continue

    def _start(self, job: PreparationJob, actor: Actor) -> None:
        events = self._process_context.Queue()
        cancel = self._process_context.Event()
        process = self._process_context.Process(
            target=_run_preparation_worker,
            args=(
                str(self.root),
                job.project_id,
                actor,
                events,
                cancel,
            ),
            name=f"impodo-preparation-{job.job_id[:8]}",
            daemon=True,
        )
        supervisor = Thread(
            target=self._supervise,
            args=(job.job_id, process, events),
            name=f"impodo-preparation-supervisor-{job.job_id[:8]}",
            daemon=True,
        )
        worker = _RunningWorker(process, events, cancel, supervisor)
        with self._lock:
            self._workers[job.job_id] = worker
        try:
            process.start()
            supervisor.start()
        except Exception:
            with self._lock:
                self._workers.pop(job.job_id, None)
            self.registry.mark_failed(
                job.job_id,
                "WORKER_START_FAILED",
                "Impodo could not start preparation. Try again.",
            )
            raise

    def _supervise(self, job_id: str, process: Any, events: Any) -> None:
        terminal_received = False
        try:
            while process.is_alive():
                try:
                    event = events.get(timeout=0.25)
                except Empty:
                    continue
                terminal_received = self._handle_event(job_id, event) or terminal_received
            process.join()
            while True:
                try:
                    event = events.get_nowait()
                except Empty:
                    break
                terminal_received = self._handle_event(job_id, event) or terminal_received
            if not terminal_received:
                current = self.registry.get_by_id(job_id)
                if current.active:
                    self.registry.mark_failed(
                        job_id,
                        "WORKER_EXITED",
                        "Preparation stopped unexpectedly. Your previous saved "
                        "evidence remains available; try again.",
                    )
        finally:
            events.close()
            with self._lock:
                self._workers.pop(job_id, None)
                self._schedule_locked()

    def _handle_event(self, job_id: str, event: tuple[Any, ...]) -> bool:
        kind = str(event[0])
        if kind == "started":
            self.registry.mark_running(job_id)
            return False
        if kind == "progress":
            self.registry.update_progress(
                job_id,
                PreparationPhase(str(event[1])),
                completed_rows=int(event[2]),
                total_rows=int(event[3]),
                message=str(event[4]),
            )
            return False
        if kind == "succeeded":
            self.registry.mark_succeeded(job_id, str(event[1]))
            return True
        if kind == "review_required":
            self.registry.mark_review_required(job_id)
            return True
        if kind == "cancelled":
            self.registry.mark_cancelled(job_id)
            return True
        if kind == "failed":
            self.registry.mark_failed(job_id, str(event[1]), str(event[2]))
            return True
        return False


def _run_preparation_worker(
    root: str,
    project_id: str,
    actor: Actor,
    events: Any,
    cancel: Any,
) -> None:
    """Child-process entry point; all messages are small control-plane events."""

    events.put(("started",))
    try:
        # Import lazily so multiprocessing can import this module without a web
        # composition cycle. The child does not create another job manager.
        from ..web.app import create_local_app

        app = create_local_app(
            root,
            actor=actor,
            preparation_jobs_enabled=False,
        )
        context = app.state.context

        def progress(
            phase: PreparationPhase,
            completed_rows: int,
            total_rows: int,
            message: str,
        ) -> None:
            events.put(
                (
                    "progress",
                    phase.value,
                    completed_rows,
                    total_rows,
                    message,
                )
            )

        def cancellation_checkpoint() -> None:
            if cancel.is_set():
                raise PreparationCancelled("Preparation cancelled")

        normalization = context.preparation.prepare(
            project_id,
            actor=actor,
            progress=progress,
            cancellation_checkpoint=cancellation_checkpoint,
        )
        events.put(("succeeded", normalization.run_id))
    except PreparationCancelled:
        events.put(("cancelled",))
    except (
        ConnectorError,
        ProjectError,
        ReadinessError,
        SecretStoreError,
        WorkspaceError,
    ) as error:
        if _resolution_review_is_waiting(locals().get("context"), project_id):
            events.put(("review_required",))
        else:
            events.put(("failed", type(error).__name__, str(error)[:1000]))
    except Exception as error:
        events.put(
            (
                "failed",
                type(error).__name__[:200],
                "Preparation stopped unexpectedly. Your previous saved evidence "
                "remains available; try again.",
            )
        )


def _resolution_review_is_waiting(context: Any, project_id: str) -> bool:
    if context is None:
        return False
    try:
        review = context.resolution.current_review(project_id)
    except Exception:
        return False
    return bool(
        review is not None
        and review.summary.status == "REVIEW_REQUIRED"
        and review.candidates
    )
