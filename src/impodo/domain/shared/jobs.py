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

    def start(self, *, at: datetime) -> JobRecord:
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
    ) -> JobRecord:
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

    def fail(self, *, at: datetime, failure_code: str) -> JobRecord:
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
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class DurableJobKind(StrEnum):
    """Allowlisted server job families migrated in explicit safety order."""

    SOURCE_INSPECTION = "source.inspect"
    ODOO_CAPTURE = "odoo.capture"
    PREPARATION = "workspace.prepare"
    PREFLIGHT = "workspace.preflight"
    RECIPE_APPLICATION = "recipe.apply"
    REPORT_CREATION = "report.create"
    LOAD = "workspace.load"
    CORRECTION = "workspace.correct"
    RECONCILIATION = "workspace.reconcile"
    GOVERNED_DELETION = "project.delete"


@dataclass(frozen=True, slots=True)
class DurableJobRequest:
    """Organization-scoped identity for a future durable execution."""

    job_id: str
    operation_id: str
    organization_id: OrganizationId
    project_id: str
    workspace_id: str | None
    job_kind: DurableJobKind
    idempotency_key: str
    semantic_input_hash: str
    manifest_hash: str
    authorization_receipt_id: str
    authorization_revision: int
    requested_by: ActorIdentity
    requested_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_kind", DurableJobKind(self.job_kind))
        for value, name in (
            (self.job_id, "job_id"),
            (self.operation_id, "operation_id"),
            (self.project_id, "project_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.authorization_receipt_id, "authorization_receipt_id"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if self.workspace_id is not None and not self.workspace_id.strip():
            raise ValueError("workspace_id must not be blank")
        _sha256_hash(self.semantic_input_hash, "semantic_input_hash")
        _sha256_hash(self.manifest_hash, "manifest_hash")
        if self.authorization_revision < 1:
            raise ValueError("authorization_revision must be positive")
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
class DurableJobAttempt:
    """Worker-bound attempt facts retained for restart reconciliation."""

    identity: JobAttemptIdentity
    worker_id: str
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None = None
    failure_code: str = ""
    result_receipt_hash: str = ""

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > 200:
            raise ValueError("worker_id must be a short non-blank identifier")
        _aware(self.started_at, "started_at")
        _aware(self.heartbeat_at, "heartbeat_at")
        if self.heartbeat_at < self.started_at:
            raise ValueError("heartbeat_at cannot precede started_at")
        if self.finished_at is not None:
            _aware(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot precede started_at")
        if self.failure_code and len(self.failure_code) > 200:
            raise ValueError("failure_code is too long")
        if self.result_receipt_hash:
            _sha256_hash(self.result_receipt_hash, "result_receipt_hash")
        if self.failure_code and self.result_receipt_hash:
            raise ValueError("attempt cannot retain both failure and result receipts")
        if self.finished_at is None and (
            self.failure_code or self.result_receipt_hash
        ):
            raise ValueError("attempt outcome requires finished_at")


@dataclass(frozen=True, slots=True)
class JobStateTransition:
    """Immutable state edge for durable recovery and audit."""

    transition_id: str
    attempt: JobAttemptIdentity
    from_state: DurableJobState
    to_state: DurableJobState
    expected_revision: int
    resulting_revision: int
    occurred_at: datetime
    reason_code: str = ""
    recovery_decision_id: str = ""

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
        if self.expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if self.resulting_revision != self.expected_revision + 1:
            raise ValueError("resulting_revision must advance by one")
        _aware(self.occurred_at, "occurred_at")
        if self.reason_code and len(self.reason_code) > 200:
            raise ValueError("reason_code is too long")
        if (
            self.to_state is DurableJobState.INTERRUPTED
            and not self.recovery_decision_id.strip()
        ):
            raise ValueError("interrupted transition requires a recovery decision")
        if self.recovery_decision_id and self.to_state not in {
            DurableJobState.INTERRUPTED,
            DurableJobState.REVIEW_REQUIRED,
            DurableJobState.SUCCEEDED,
        }:
            raise ValueError(
                "recovery decision can produce only interrupted, review, or success"
            )


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
        if self.total_units is not None and (
            self.total_units < 0 or self.completed_units > self.total_units
        ):
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


class JobCancellationDisposition(StrEnum):
    """Bounded outcome of one cooperative cancellation request."""

    ACCEPTED = "ACCEPTED"
    DEFERRED_UNSAFE_BOUNDARY = "DEFERRED_UNSAFE_BOUNDARY"
    COMPLETED_BEFORE_STOP = "COMPLETED_BEFORE_STOP"
    REJECTED_TERMINAL = "REJECTED_TERMINAL"


@dataclass(frozen=True, slots=True)
class JobCancellationReceipt:
    """Retain cancellation disposition without deleting job history."""

    cancellation_id: str
    attempt: JobAttemptIdentity
    disposition: JobCancellationDisposition
    disposed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "disposition",
            JobCancellationDisposition(self.disposition),
        )
        if not self.cancellation_id.strip():
            raise ValueError("cancellation_id must not be blank")
        _aware(self.disposed_at, "disposed_at")


class JobReceiptCheckStatus(StrEnum):
    """Bounded result of checking one expired attempt's data-plane receipt."""

    NO_COMMITTED_RECEIPT = "NO_COMMITTED_RECEIPT"
    MATCHED_COMMITTED_RECEIPT = "MATCHED_COMMITTED_RECEIPT"
    RECEIPT_MISMATCH = "RECEIPT_MISMATCH"
    CHECK_UNAVAILABLE = "CHECK_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class JobReceiptCheck:
    """Prove the data-plane state observed after one attempt expired."""

    check_id: str
    attempt: JobAttemptIdentity
    operation_id: str
    status: JobReceiptCheckStatus
    attempt_expired_at: datetime
    checked_at: datetime
    data_plane_receipt_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobReceiptCheckStatus(self.status))
        for value, name in (
            (self.check_id, "check_id"),
            (self.operation_id, "operation_id"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        _aware(self.attempt_expired_at, "attempt_expired_at")
        _aware(self.checked_at, "checked_at")
        if self.checked_at < self.attempt_expired_at:
            raise ValueError("receipt check cannot precede attempt expiry")
        receipt_observed = self.status in {
            JobReceiptCheckStatus.MATCHED_COMMITTED_RECEIPT,
            JobReceiptCheckStatus.RECEIPT_MISMATCH,
        }
        if receipt_observed:
            _sha256_hash(
                self.data_plane_receipt_hash,
                "data_plane_receipt_hash",
            )
        elif self.data_plane_receipt_hash:
            raise ValueError("receipt hash requires an observed data-plane receipt")


class JobRecoveryDisposition(StrEnum):
    """Allowed recovery action after checking an expired attempt."""

    INTERRUPT_RETRYABLE = "INTERRUPT_RETRYABLE"
    COMPLETE_FROM_RECEIPT = "COMPLETE_FROM_RECEIPT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class JobRecoveryDecision:
    """Bind one expired-attempt decision to its exact receipt check."""

    decision_id: str
    receipt_check: JobReceiptCheck
    disposition: JobRecoveryDisposition
    decided_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "disposition",
            JobRecoveryDisposition(self.disposition),
        )
        if not self.decision_id.strip():
            raise ValueError("decision_id must not be blank")
        _aware(self.decided_at, "decided_at")
        if self.decided_at < self.receipt_check.checked_at:
            raise ValueError("recovery decision cannot precede its receipt check")
        if not self.reason_code.strip() or len(self.reason_code) > 200:
            raise ValueError("reason_code must be a short non-blank code")
        allowed = {
            JobReceiptCheckStatus.NO_COMMITTED_RECEIPT: (
                JobRecoveryDisposition.INTERRUPT_RETRYABLE
            ),
            JobReceiptCheckStatus.MATCHED_COMMITTED_RECEIPT: (
                JobRecoveryDisposition.COMPLETE_FROM_RECEIPT
            ),
            JobReceiptCheckStatus.RECEIPT_MISMATCH: (
                JobRecoveryDisposition.REVIEW_REQUIRED
            ),
            JobReceiptCheckStatus.CHECK_UNAVAILABLE: (
                JobRecoveryDisposition.REVIEW_REQUIRED
            ),
        }
        if self.disposition is not allowed[self.receipt_check.status]:
            raise ValueError(
                "recovery disposition does not match the receipt check"
            )


@dataclass(frozen=True, slots=True)
class JobInterruption:
    """Prove an expired attempt is retryable only after a receipt check."""

    interruption_id: str
    attempt: JobAttemptIdentity
    recovery_decision: JobRecoveryDecision
    interrupted_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        if not self.interruption_id.strip():
            raise ValueError("interruption_id must not be blank")
        if self.attempt != self.recovery_decision.receipt_check.attempt:
            raise ValueError("interruption attempt must match its recovery decision")
        if (
            self.recovery_decision.disposition
            is not JobRecoveryDisposition.INTERRUPT_RETRYABLE
        ):
            raise ValueError("interruption requires a retryable recovery decision")
        if not self.reason_code.strip() or len(self.reason_code) > 200:
            raise ValueError("reason_code must be a short non-blank code")
        _aware(self.interrupted_at, "interrupted_at")
        if self.interrupted_at < self.recovery_decision.decided_at:
            raise ValueError("interruption cannot precede its recovery decision")


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
            DurableJobState.REVIEW_REQUIRED,
            DurableJobState.SUCCEEDED,
            DurableJobState.FAILED,
            DurableJobState.CANCELLED,
            DurableJobState.INTERRUPTED,
        }
    ),
    DurableJobState.REVIEW_REQUIRED: frozenset(),
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
