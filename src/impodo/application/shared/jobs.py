"""Application-facing job dispatch port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from impodo.domain.shared.jobs import JobRecord, JobRequest


JobWork = Callable[[], tuple[str, ...]]


class JobDispatcher(Protocol):
    """Port for idempotent execution and later status lookup."""

    def dispatch(self, request: JobRequest, work: JobWork) -> JobRecord:
        """Execute or reuse the request bound to its idempotency key."""
        ...

    def get(self, job_id: str) -> JobRecord:
        """Return the latest lifecycle record for ``job_id``."""
        ...
