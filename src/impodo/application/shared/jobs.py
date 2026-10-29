"""Application-facing job dispatch port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from impodo.domain.shared.jobs import (
    DurableJobAttempt,
    DurableJobRequest,
    JobCancellation,
    JobCancellationReceipt,
    JobInterruption,
    JobProgress,
    JobRecord,
    JobRecoveryDecision,
    JobRequest,
    JobResultIdentity,
    JobStateTransition,
)

JobWork = Callable[[], tuple[str, ...]]


class JobDispatcher(Protocol):
    """Port for idempotent execution and later status lookup."""

    def dispatch(self, request: JobRequest, work: JobWork) -> JobRecord:
        """Execute or reuse the request bound to its idempotency key."""
        ...

    def get(self, job_id: str) -> JobRecord:
        """Return the latest lifecycle record for ``job_id``."""
        ...


class DurableJobControl(Protocol):
    """Persist future shared-server job lifecycle facts."""

    def enqueue(self, request: DurableJobRequest) -> DurableJobRequest:
        """Create or reuse one exact durable job request."""
        ...

    def record_attempt(self, attempt: DurableJobAttempt) -> None:
        """Persist one worker identity, fence, and bounded heartbeat."""
        ...

    def record_transition(self, transition: JobStateTransition) -> None:
        """Append one valid compare-and-swap lifecycle transition."""
        ...

    def record_progress(self, progress: JobProgress) -> None:
        """Replace bounded progress for the current attempt and token."""
        ...

    def request_cancellation(self, cancellation: JobCancellation) -> None:
        """Record cooperative cancellation for the current attempt."""
        ...

    def record_cancellation_disposition(
        self,
        receipt: JobCancellationReceipt,
    ) -> None:
        """Retain the cooperative cancellation outcome in job history."""
        ...

    def record_recovery_decision(self, decision: JobRecoveryDecision) -> None:
        """Retain the receipt-backed decision for one expired attempt."""
        ...

    def record_interruption(self, interruption: JobInterruption) -> None:
        """Interrupt only an expired attempt proven to have no receipt."""
        ...

    def record_result(self, result: JobResultIdentity) -> None:
        """Bind terminal success to one accepted data-plane receipt."""
        ...
