"""Synchronous local job dispatcher."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from impodo.application.shared.jobs import JobWork
from impodo.domain.shared.jobs import JobRecord, JobRequest


class InlineJobDispatcher:
    """Execute work locally while preserving hosted idempotency semantics."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_job_id: dict[str, JobRecord] = {}
        self._by_idempotency: dict[str, str] = {}

    def dispatch(self, request: JobRequest, work: JobWork) -> JobRecord:
        """Run work synchronously while enforcing idempotent request binding."""

        with self._lock:
            existing_id = self._by_idempotency.get(request.idempotency_key)
            if existing_id is not None:
                existing = self._by_job_id[existing_id]
                original = existing.request
                if (
                    original.workspace_id != request.workspace_id
                    or original.kind is not request.kind
                    or original.input_hash != request.input_hash
                    or original.requested_by != request.requested_by
                ):
                    raise ValueError(
                        "idempotency_key is bound to a different job request"
                    )
                return existing
            if request.job_id in self._by_job_id:
                raise ValueError("job_id is already registered")
            running = JobRecord(request).start(at=datetime.now(timezone.utc))
            self._by_job_id[request.job_id] = running
            self._by_idempotency[request.idempotency_key] = request.job_id

        try:
            artifact_ids = work()
        except Exception as error:
            failed = running.fail(
                at=datetime.now(timezone.utc),
                failure_code=type(error).__name__,
            )
            with self._lock:
                self._by_job_id[request.job_id] = failed
            raise

        succeeded = running.succeed(
            at=datetime.now(timezone.utc),
            result_artifact_ids=artifact_ids,
        )
        with self._lock:
            self._by_job_id[request.job_id] = succeeded
        return succeeded

    def get(self, job_id: str) -> JobRecord:
        """Return the last immutable record stored for a local job."""

        with self._lock:
            try:
                return self._by_job_id[job_id]
            except KeyError as error:
                raise KeyError("job not found") from error
