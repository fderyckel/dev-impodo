"""Run heavy preparation in a child process and supervise live progress."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from multiprocessing.context import BaseContext
from pathlib import Path
from queue import Empty
from threading import RLock, Thread
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable

import duckdb

from impodo.domain.shared.access import Actor
from impodo.application.shared.build_contract import (
    ApplicationBuildContract,
    ApplicationBuildMismatchError,
    PROCESS_BUILD_CONTRACT,
    require_same_application_build,
)
from impodo.application.workspace.preparation.preparation_job_registry import (
    PreparationJobRegistry,
    PreparationJobStateError,
)
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.errors import ReadinessError
from impodo.application.workspace.preparation.job_models import (
    PreparationJob,
    PreparationJobStatus,
    PreparationPhase,
    PreparationWorkspace,
)
from impodo.domain.workspace.workbench import WorkspaceStateError
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.workspace.errors import WorkspaceError

if TYPE_CHECKING:
    from ..diagnostics import LocalDiagnosticRecorder


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
        build_contract: ApplicationBuildContract = PROCESS_BUILD_CONTRACT,
        status_listener: Callable[[PreparationJob], None] | None = None,
        diagnostic_recorder: LocalDiagnosticRecorder | None = None,
    ) -> None:
        import multiprocessing

        self.root = Path(root).resolve()
        self.registry = PreparationJobRegistry()
        self.build_contract = build_contract
        self._process_context = process_context or multiprocessing.get_context("spawn")
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        self._max_workers = max_workers
        self._lock = RLock()
        self._workers: dict[str, _RunningWorker] = {}
        self._pending: dict[str, tuple[PreparationJob, Actor]] = {}
        self._status_listener = status_listener
        self._diagnostic_recorder = diagnostic_recorder

    def set_status_listener(
        self,
        listener: Callable[[PreparationJob], None] | None,
    ) -> None:
        """Publish coarse run progress without coupling it to worker evidence."""

        self._status_listener = listener

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        total_rows: int,
        *,
        actor: Actor,
        workspace: PreparationWorkspace,
    ) -> PreparationJob:
        """Register one attempt and start it without holding the HTTP request."""

        job, created = self.registry.enqueue(
            workspace_id,
            migration_project_name,
            total_rows,
            actor.identity,
            workspace,
            self.build_contract,
        )
        if created:
            with self._lock:
                self._pending[job.job_id] = (job, actor)
                self._schedule_locked()
        return job

    def retry(
        self,
        workspace_id: str,
        job_id: str,
        migration_project_name: str,
        total_rows: int,
        *,
        actor: Actor,
        workspace: PreparationWorkspace,
    ) -> PreparationJob:
        """Create a fresh attempt after a failed or cancelled job."""

        previous = self.registry.get(workspace_id, job_id)
        if previous.status not in {
            PreparationJobStatus.FAILED,
            PreparationJobStatus.CANCELLED,
        }:
            raise PreparationJobStateError(
                "Only a failed or stopped preparation can be tried again"
            )
        if not previous.retry_allowed:
            raise PreparationJobStateError(
                "Restart Impodo before preparing this saved work again"
            )
        return self.enqueue(
            workspace_id,
            migration_project_name,
            total_rows,
            actor=actor,
            workspace=workspace,
        )

    def get(self, workspace_id: str, job_id: str) -> PreparationJob:
        return self.registry.get(workspace_id, job_id)

    def active(self, workspace_id: str) -> PreparationJob | None:
        return self.registry.active(workspace_id)

    def latest_many(
        self,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, PreparationJob]:
        return self.registry.latest_many(workspace_ids)

    def delete_workspace_history(self, workspace_id: str) -> None:
        self.registry.delete_workspace_history(workspace_id)

    def cancel(self, workspace_id: str, job_id: str) -> PreparationJob:
        job = self.registry.request_cancel(workspace_id, job_id)
        with self._lock:
            worker = self._workers.get(job_id)
            if worker is not None:
                worker.cancel.set()
                return job
            if self._pending.pop(job_id, None) is not None:
                stopped = self.registry.mark_cancelled(job_id)
                self._notify(stopped)
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
                # one launch problem cannot strand unrelated queued workspaces.
                continue

    def _start(self, job: PreparationJob, actor: Actor) -> None:
        events = self._process_context.Queue()
        cancel = self._process_context.Event()
        process = self._process_context.Process(
            target=_run_preparation_worker,
            args=(
                str(self.root),
                job.workspace_id,
                job.workspace,
                job.build_contract,
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
            self._notify(
                self.registry.mark_failed(
                    job.job_id,
                    "WORKER_START_FAILED",
                    "Impodo could not start preparation. Try again.",
                )
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
                    self._notify(
                        self.registry.mark_failed(
                            job_id,
                            "WORKER_EXITED",
                            "Preparation stopped unexpectedly. Your previous saved "
                            "evidence remains available; try again.",
                        )
                    )
        finally:
            events.close()
            with self._lock:
                self._workers.pop(job_id, None)
                self._schedule_locked()

    def _handle_event(self, job_id: str, event: tuple[Any, ...]) -> bool:
        kind = str(event[0])
        if kind == "started":
            self._notify(self.registry.mark_running(job_id))
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
        if kind == "timing":
            self._record_timing_event(event)
            return False
        if kind == "succeeded":
            self._notify(self.registry.mark_succeeded(job_id, str(event[1])))
            return True
        if kind == "review_required":
            self._notify(self.registry.mark_review_required(job_id))
            return True
        if kind == "cancelled":
            self._notify(self.registry.mark_cancelled(job_id))
            return True
        if kind == "failed":
            self._notify(
                self.registry.mark_failed(job_id, str(event[1]), str(event[2]))
            )
            return True
        return False

    def _record_timing_event(self, event: tuple[Any, ...]) -> None:
        """Persist value-free worker timing without affecting job truth."""

        if self._diagnostic_recorder is None:
            return
        try:
            self._diagnostic_recorder.record_operation_stage(
                "preparation",
                str(event[1]),
                duration_ms=float(event[2]),
                outcome=str(event[3]),
                reason=(str(event[4]) if event[4] else None),
                completed_rows=int(event[5]),
                total_rows=int(event[6]),
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not record preparation timing diagnostics"
            )

    def _notify(self, job: PreparationJob) -> None:
        """Keep a projection failure from changing preparation truth."""

        if self._status_listener is None:
            return
        try:
            self._status_listener(job)
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not publish Recipe preparation progress"
            )


def _run_preparation_worker(
    root: str,
    workspace_id: str,
    workspace: PreparationWorkspace,
    expected_build_contract: ApplicationBuildContract,
    actor: Actor,
    events: Any,
    cancel: Any,
) -> None:
    """Child-process entry point; all messages are small control-plane events."""

    events.put(("started",))
    overall_started = perf_counter()
    active_phase: PreparationPhase | None = None
    active_phase_started = overall_started
    completed_rows = 0
    total_rows = 0
    timing_finished = False

    def emit_timing(
        stage: str,
        duration_ms: float,
        outcome: str,
        reason: str = "",
    ) -> None:
        events.put(
            (
                "timing",
                stage,
                max(0.0, duration_ms),
                outcome,
                reason,
                completed_rows,
                total_rows,
            )
        )

    def progress(
        phase: PreparationPhase,
        current_completed_rows: int,
        current_total_rows: int,
        message: str,
    ) -> None:
        nonlocal active_phase, active_phase_started, completed_rows, total_rows
        observed_at = perf_counter()
        if phase is not active_phase:
            if active_phase is not None:
                emit_timing(
                    active_phase.value,
                    (observed_at - active_phase_started) * 1000,
                    "completed",
                )
            active_phase = (
                None if phase is PreparationPhase.COMPLETE else phase
            )
            active_phase_started = observed_at
        completed_rows = max(0, int(current_completed_rows))
        total_rows = max(0, int(current_total_rows))
        events.put(
            (
                "progress",
                phase.value,
                completed_rows,
                total_rows,
                message,
            )
        )

    def diagnostic_timing(stage: str, duration_ms: float, outcome: str) -> None:
        emit_timing(stage, duration_ms, outcome)

    def finish_timing(outcome: str, reason: str = "") -> None:
        nonlocal active_phase, timing_finished
        if timing_finished:
            return
        observed_at = perf_counter()
        if active_phase is not None:
            emit_timing(
                active_phase.value,
                (observed_at - active_phase_started) * 1000,
                outcome,
                reason,
            )
            active_phase = None
        emit_timing(
            "total",
            (observed_at - overall_started) * 1000,
            outcome,
            reason,
        )
        timing_finished = True

    try:
        require_same_application_build(expected_build_contract)
        # Import lazily so multiprocessing imports only the workspace-scoped
        # worker composition. The child never opens the Recipe registry.
        from impodo.web.composition.preparation_worker import create_preparation_worker

        preparation = create_preparation_worker(root, workspace=workspace)

        def cancellation_checkpoint() -> None:
            if cancel.is_set():
                raise PreparationCancelled("Preparation cancelled")

        normalization = preparation.prepare(
            workspace_id,
            actor=actor,
            progress=progress,
            cancellation_checkpoint=cancellation_checkpoint,
            timing=diagnostic_timing,
            expected_mapping_hash=workspace.mapping_content_hash,
        )
        finish_timing("succeeded")
        events.put(("succeeded", normalization.run_id))
    except PreparationCancelled:
        finish_timing("cancelled", "PREPARATION_CANCELLED")
        events.put(("cancelled",))
    except ApplicationBuildMismatchError as error:
        finish_timing("failed", error.failure_code)
        events.put(("failed", error.failure_code, str(error)))
    except (
        ConnectorError,
        WorkspaceStateError,
        ReadinessError,
        SecretStoreError,
        WorkspaceError,
    ) as error:
        if _resolution_review_is_waiting(locals().get("preparation"), workspace_id):
            finish_timing("review_required", "RESOLUTION_REVIEW_REQUIRED")
            events.put(("review_required",))
        else:
            failure_code = str(
                getattr(
                    error,
                    "failure_code",
                    type(error).__name__,
                )
            )[:200]
            finish_timing("failed", failure_code)
            events.put(
                (
                    "failed",
                    failure_code,
                    str(error)[:1000],
                )
            )
    except duckdb.IOException:
        finish_timing("failed", "LOCAL_WORKSPACE_STORAGE_IO_FAILED")
        events.put(
            (
                "failed",
                "LOCAL_WORKSPACE_STORAGE_IO_FAILED",
                "Impodo could not read or save this workspace's local files. "
                "No Odoo records were changed and your previous prepared "
                "evidence remains available. Check that the local drive is "
                "available and has free space, then try again.",
            )
        )
    except Exception as error:
        failure_code = type(error).__name__[:200]
        finish_timing("failed", failure_code)
        events.put(
            (
                "failed",
                failure_code,
                "Preparation stopped unexpectedly. Your previous saved evidence "
                "remains available; try again.",
            )
        )


def _resolution_review_is_waiting(preparation: Any, workspace_id: str) -> bool:
    if preparation is None or preparation.resolution is None:
        return False
    try:
        review = preparation.resolution.current_review(workspace_id)
    except Exception:
        return False
    return bool(
        review is not None
        and review.summary.status == "REVIEW_REQUIRED"
        and review.candidates
    )
