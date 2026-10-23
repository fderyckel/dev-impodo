"""Portable job request and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.tenancy import OrganizationId


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


class DurableJobState(StrEnum):
    """Server-portable lifecycle for one durable job attempt."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class DurableJobRequest:
    """Organization-scoped identity for a future durable execution."""

    job_id: str
    operation_id: str
    organization_id: OrganizationId
    project_id: str
    workspace_id: str | None
    job_kind: str
    idempotency_key: str
    semantic_input_hash: str
    authorization_receipt_id: str
    requested_by: ActorIdentity
    requested_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "job_id"),
            (self.operation_id, "operation_id"),
            (self.project_id, "project_id"),
            (self.job_kind, "job_kind"),
            (self.idempotency_key, "idempotency_key"),
            (self.authorization_receipt_id, "authorization_receipt_id"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if self.workspace_id is not None and not self.workspace_id.strip():
            raise ValueError("workspace_id must not be blank")
        _sha256_hash(self.semantic_input_hash, "semantic_input_hash")
        _aware(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class JobAttemptIdentity:
    """Stable identity and fencing token for one execution attempt."""

    attempt_id: str
    job_id: str
    attempt_number: int
    fencing_token: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.attempt_id, "attempt_id"),
            (self.job_id, "job_id"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")


@dataclass(frozen=True, slots=True)
class JobStateTransition:
    """Immutable state edge for durable recovery and audit."""

    transition_id: str
    attempt: JobAttemptIdentity
    from_state: DurableJobState
    to_state: DurableJobState
    occurred_at: datetime
    reason_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_state", DurableJobState(self.from_state))
        object.__setattr__(self, "to_state", DurableJobState(self.to_state))
        if not self.transition_id.strip():
            raise ValueError("transition_id must not be blank")
        if self.from_state is self.to_state:
            raise ValueError("job transition must change state")
        if self.to_state not in _DURABLE_JOB_TRANSITIONS[self.from_state]:
            raise ValueError(
                f"invalid job transition {self.from_state.value} -> "
                f"{self.to_state.value}"
            )
        _aware(self.occurred_at, "occurred_at")
        if self.reason_code and len(self.reason_code) > 200:
            raise ValueError("reason_code is too long")


@dataclass(frozen=True, slots=True)
class JobProgress:
    """Bounded progress projection without source or target values."""

    attempt: JobAttemptIdentity
    phase_code: str
    completed_units: int
    total_units: int | None
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.phase_code.strip() or len(self.phase_code) > 200:
            raise ValueError("phase_code must be a short non-blank code")
        if self.completed_units < 0:
            raise ValueError("completed_units must not be negative")
        if self.total_units is not None:
            if self.total_units < 0 or self.completed_units > self.total_units:
                raise ValueError("progress units exceed the bounded total")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class JobCancellation:
    """Cooperative cancellation requested for one current attempt."""

    cancellation_id: str
    attempt: JobAttemptIdentity
    requested_by: ActorIdentity
    requested_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        if not self.cancellation_id.strip():
            raise ValueError("cancellation_id must not be blank")
        if not self.reason_code.strip() or len(self.reason_code) > 200:
            raise ValueError("reason_code must be a short non-blank code")
        _aware(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class JobInterruption:
    """Bounded evidence that a non-terminal attempt lost its worker."""

    interruption_id: str
    attempt: JobAttemptIdentity
    interrupted_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        if not self.interruption_id.strip():
            raise ValueError("interruption_id must not be blank")
        if not self.reason_code.strip() or len(self.reason_code) > 200:
            raise ValueError("reason_code must be a short non-blank code")
        _aware(self.interrupted_at, "interrupted_at")


@dataclass(frozen=True, slots=True)
class JobResultIdentity:
    """Result pointer bound to a committed data-plane receipt."""

    result_id: str
    attempt: JobAttemptIdentity
    data_plane_receipt_hash: str
    result_artifact_ids: tuple[str, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        if not self.result_id.strip():
            raise ValueError("result_id must not be blank")
        _sha256_hash(self.data_plane_receipt_hash, "data_plane_receipt_hash")
        artifacts = tuple(sorted(set(self.result_artifact_ids)))
        if any(not item.strip() for item in artifacts):
            raise ValueError("result_artifact_ids must not contain blanks")
        object.__setattr__(self, "result_artifact_ids", artifacts)
        _aware(self.completed_at, "completed_at")


_DURABLE_JOB_TRANSITIONS = {
    DurableJobState.QUEUED: frozenset(
        {
            DurableJobState.RUNNING,
            DurableJobState.CANCELLED,
        }
    ),
    DurableJobState.RUNNING: frozenset(
        {
            DurableJobState.SUCCEEDED,
            DurableJobState.FAILED,
            DurableJobState.CANCELLED,
            DurableJobState.INTERRUPTED,
        }
    ),
    DurableJobState.INTERRUPTED: frozenset(),
    DurableJobState.SUCCEEDED: frozenset(),
    DurableJobState.FAILED: frozenset(),
    DurableJobState.CANCELLED: frozenset(),
}


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
