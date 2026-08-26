"""Portable job request and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from impodo.domain.shared.access import ActorIdentity


class JobKind(StrEnum):
    """Allowlisted long-running operations that may use a job boundary."""

    SOURCE_INSPECTION = "SOURCE_INSPECTION"
    NORMALIZATION = "NORMALIZATION"
    PREFLIGHT = "PREFLIGHT"
    REPORT = "REPORT"


class JobStatus(StrEnum):
    """Lifecycle states for one idempotent job request."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class JobRequest:
    """Immutable workspace identity, provenance, and semantic job input."""

    job_id: str
    workspace_id: str
    kind: JobKind
    idempotency_key: str
    input_hash: str
    requested_by: ActorIdentity
    requested_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "job_id"),
            (self.workspace_id, "workspace_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        _sha256_hash(self.input_hash, "input_hash")
        if self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Versioned job state; transition methods return replaced instances."""

    request: JobRequest
    status: JobStatus = JobStatus.QUEUED
    version: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_artifact_ids: tuple[str, ...] = ()
    failure_code: str = ""

    def start(self, *, at: datetime) -> "JobRecord":
        """Move a queued job to running at a timezone-aware timestamp."""

        self._require(JobStatus.QUEUED)
        _aware(at, "started_at")
        return replace(
            self,
            status=JobStatus.RUNNING,
            version=self.version + 1,
            started_at=at,
        )

    def succeed(
        self,
        *,
        at: datetime,
        result_artifact_ids: tuple[str, ...] = (),
    ) -> "JobRecord":
        """Complete a running job and retain its result artifact identities."""

        self._require(JobStatus.RUNNING)
        _aware(at, "finished_at")
        return replace(
            self,
            status=JobStatus.SUCCEEDED,
            version=self.version + 1,
            finished_at=at,
            result_artifact_ids=tuple(result_artifact_ids),
        )

    def fail(self, *, at: datetime, failure_code: str) -> "JobRecord":
        """Complete a running job with a bounded non-sensitive failure code."""

        self._require(JobStatus.RUNNING)
        _aware(at, "finished_at")
        if not failure_code.strip() or len(failure_code) > 200:
            raise ValueError("failure_code must be a short non-blank code")
        return replace(
            self,
            status=JobStatus.FAILED,
            version=self.version + 1,
            finished_at=at,
            failure_code=failure_code,
        )

    def _require(self, expected: JobStatus) -> None:
        if self.status is not expected:
            raise ValueError(
                f"job must be {expected.value}; current status is {self.status.value}"
            )


def _sha256_hash(value: str, name: str) -> str:
    digest = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(digest) != 64:
        raise ValueError(f"{name} must use sha256:<64 hex characters>")
    try:
        int(digest, 16)
    except ValueError as error:
        raise ValueError(f"{name} must use sha256:<64 hex characters>") from error
    return value


def _aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
