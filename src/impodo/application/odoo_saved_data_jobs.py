"""Serialize saved-data refresh and restore jobs without retaining database owners."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from impodo.application.shared.serial_job_runner import SerialJobRunner


@dataclass(frozen=True, slots=True)
class SavedOdooJob:
    """Expose progress without source values, Odoo identifiers, or credentials."""

    job_id: str
    project_id: str
    saved_data_id: str
    kind: str
    status: str
    progress_percent: int
    message: str
    redirect_url: str
    created_at: datetime
    failure_message: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in {"SUCCEEDED", "FAILED"}


class SavedOdooDataJobManager:
    """Keep one active action per saved configuration and release work on shutdown."""

    def __init__(self) -> None:
        self._runner = SerialJobRunner(worker_name="impodo-saved-odoo-data")
        self._lock = RLock()
        self._jobs: dict[str, SavedOdooJob] = {}

    def enqueue(
        self, project_id: str, saved_data_id: str, *, kind: str,
        work: Callable[[Callable[[int, str], None]], str],
    ) -> SavedOdooJob:
        """Return an active job or enqueue one explicitly requested action."""

        with self._lock:
            active = next((job for job in self._jobs.values() if job.saved_data_id == saved_data_id and job.project_id == project_id and not job.terminal), None)
            if active is not None:
                return active
            job = SavedOdooJob(str(uuid4()), project_id, saved_data_id, kind, "QUEUED", 0, "Waiting to start", "", datetime.now(UTC))
            self._jobs[job.job_id] = job
            try:
                self._runner.submit(job.job_id, lambda: self._execute(job.job_id, work), discard=lambda: self._fail(job.job_id, "Impodo stopped before this action started. Your saved versions remain available."))
            except Exception:
                del self._jobs[job.job_id]
                raise
            return job

    def get(self, project_id: str, saved_data_id: str, job_id: str) -> SavedOdooJob:
        """Reject a progress identifier outside its owning project and definition."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.project_id != project_id or job.saved_data_id != saved_data_id:
                raise LookupError("Saved Odoo data action not found")
            return job

    def shutdown(self) -> None:
        """Stop queued actions while preserving immutable captured data."""

        self._runner.shutdown()

    def _execute(self, job_id: str, work: Callable[[Callable[[int, str], None]], str]) -> None:
        self._progress(job_id, 1, "Checking the saved configuration")
        try:
            location = work(lambda percent, message: self._progress(job_id, percent, message))
        except Exception:  # noqa: BLE001 - background worker records safe errors only
            self._fail(job_id, "The action stopped. Review the saved data and destination before retrying. Existing saved versions remain available.")
        else:
            with self._lock:
                self._jobs[job_id] = replace(self._jobs[job_id], status="SUCCEEDED", progress_percent=100, message="Action complete", redirect_url=location)

    def _progress(self, job_id: str, percent: int, message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._jobs[job_id] = replace(job, status="RUNNING", progress_percent=max(job.progress_percent, min(99, max(1, percent))), message=message[:300])

    def _fail(self, job_id: str, message: str) -> None:
        with self._lock:
            self._jobs[job_id] = replace(self._jobs[job_id], status="FAILED", message="Action needs attention", failure_message=message)
