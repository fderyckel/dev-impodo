"""Portable cross-store operation, lease, receipt, and conflict contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath

from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.tenancy import OrganizationId


class StaleLeaseError(RuntimeError):
    """Raised when a worker no longer owns the active fencing token."""


class ReceiptMismatchError(RuntimeError):
    """Raised when a projection is paired with substituted data-plane evidence."""


class IdempotencyConflictError(ValueError):
    """Raised when one Organization reuses a key for different semantic work."""


class OperationNotFoundError(LookupError):
    """Raised when an Organization-scoped operation does not exist."""


class OperationStateConflictError(RuntimeError):
    """Raised when recovery cannot change a terminal or newer operation."""


class CrossStoreOperationState(StrEnum):
    """Durable control-plane state for one cross-store operation."""

    RESERVED = "RESERVED"
    DATA_COMMITTED = "DATA_COMMITTED"
    PROJECTED = "PROJECTED"
    INTERRUPTED = "INTERRUPTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class RecoveryDisposition(StrEnum):
    """Bounded reason for a terminal recovery decision."""

    NO_COMMITTED_RECEIPT = "NO_COMMITTED_RECEIPT"
    RECEIPT_MISMATCH = "RECEIPT_MISMATCH"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    PROJECTION_MISMATCH = "PROJECTION_MISMATCH"


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Storage-neutral identity for one immutable governed artifact."""

    artifact_id: str
    owner_kind: str
    owner_id: str
    artifact_kind: str
    storage_key: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "owner_kind",
            "owner_id",
            "artifact_kind",
            "storage_key",
            "media_type",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=500),
            )
        path = PurePosixPath(self.storage_key)
        if path.is_absolute() or ".." in path.parts or "\\" in self.storage_key:
            raise ValueError("storage_key must be a contained portable key")
        _sha256_hash(self.sha256, "sha256")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """Idempotent command identity shared by control and data planes."""

    operation_id: str
    organization_id: OrganizationId
    project_id: str
    operation_kind: str
    idempotency_key: str
    semantic_input_hash: str
    authorization_receipt_id: str
    requested_by: ActorIdentity
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "project_id",
            "operation_kind",
            "idempotency_key",
            "authorization_receipt_id",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=300),
            )
        _sha256_hash(self.semantic_input_hash, "semantic_input_hash")
        _aware(self.requested_at, "requested_at")

    @property
    def idempotency_scope(self) -> tuple[OrganizationId, str]:
        """Return the server-wide reservation key for this Organization."""

        return (self.organization_id, self.idempotency_key)

    def require_same_idempotent_request(self, other: OperationRequest) -> None:
        """Reject reuse of one key for different semantic work."""

        if self.idempotency_scope != other.idempotency_scope:
            raise IdempotencyConflictError(
                "operation requests do not share an idempotency scope"
            )
        if (
            self.project_id != other.project_id
            or self.operation_kind != other.operation_kind
            or self.semantic_input_hash != other.semantic_input_hash
        ):
            raise IdempotencyConflictError(
                "idempotency key was reused for different semantic input"
            )


@dataclass(frozen=True, slots=True)
class OperationReservation:
    """Say whether one semantic reservation was newly created or replayed."""

    request: OperationRequest
    created: bool


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """Exclusive Workspace writer lease with a monotonic fencing token."""

    lease_id: str
    organization_id: OrganizationId
    project_id: str
    workspace_id: str
    holder_attempt_id: str
    fencing_token: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    version: int = 1
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "lease_id",
            "project_id",
            "workspace_id",
            "holder_attempt_id",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=200),
            )
        for value, name in (
            (self.acquired_at, "acquired_at"),
            (self.heartbeat_at, "heartbeat_at"),
            (self.expires_at, "expires_at"),
        ):
            _aware(value, name)
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if self.version < 1:
            raise ValueError("version must be positive")
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError(
                "lease timestamps must satisfy acquired <= heartbeat < expires"
            )
        if self.released_at is not None:
            _aware(self.released_at, "released_at")
            if self.released_at < self.heartbeat_at:
                raise ValueError("released_at must not precede the last heartbeat")

    def is_active_at(self, at: datetime) -> bool:
        """Return whether this lease still protects writes at ``at``."""

        _aware(at, "at")
        return self.released_at is None and at < self.expires_at

    def require_fencing_token(self, fencing_token: int, *, at: datetime) -> None:
        """Reject expired, released, or superseded write ownership."""

        if fencing_token != self.fencing_token or not self.is_active_at(at):
            raise StaleLeaseError("Workspace lease is expired, released, or fenced")

    def heartbeat(
        self,
        *,
        fencing_token: int,
        at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        """Extend a current lease without changing its fencing token."""

        self.require_fencing_token(fencing_token, at=at)
        _aware(expires_at, "expires_at")
        if at < self.heartbeat_at:
            raise ValueError("heartbeat cannot move backward")
        if expires_at <= self.expires_at or expires_at <= at:
            raise ValueError("heartbeat must extend the lease expiry")
        return replace(
            self,
            heartbeat_at=at,
            expires_at=expires_at,
            version=self.version + 1,
        )

    def release(self, *, fencing_token: int, at: datetime) -> WorkspaceLease:
        """Release a current lease explicitly."""

        self.require_fencing_token(fencing_token, at=at)
        if at < self.heartbeat_at:
            raise ValueError("release cannot precede the last heartbeat")
        return replace(self, released_at=at, version=self.version + 1)

    def supersedes(self, other: WorkspaceLease) -> bool:
        """Return whether this lease fences an older lease for the same Workspace."""

        return (
            self.organization_id == other.organization_id
            and self.project_id == other.project_id
            and self.workspace_id == other.workspace_id
            and self.fencing_token > other.fencing_token
        )


@dataclass(frozen=True, slots=True)
class DataPlaneOperationReceipt:
    """Immutable proof that one fenced data-plane command committed."""

    receipt_id: str
    operation_id: str
    organization_id: OrganizationId
    project_id: str
    workspace_id: str | None
    operation_kind: str
    semantic_input_hash: str
    fencing_token: int | None
    evidence_hash: str
    artifacts: tuple[ArtifactIdentity, ...]
    committed_at: datetime

    def __post_init__(self) -> None:
        for name in ("receipt_id", "operation_id", "project_id", "operation_kind"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=300),
            )
        if self.workspace_id is not None:
            object.__setattr__(
                self,
                "workspace_id",
                _required_text(self.workspace_id, "workspace_id", maximum=200),
            )
        _sha256_hash(self.semantic_input_hash, "semantic_input_hash")
        _sha256_hash(self.evidence_hash, "evidence_hash")
        if self.fencing_token is not None and self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        object.__setattr__(
            self,
            "artifacts",
            tuple(sorted(self.artifacts, key=lambda item: item.artifact_id)),
        )
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact identities must be unique")
        _aware(self.committed_at, "committed_at")

    @property
    def semantic_hash(self) -> str:
        """Return canonical receipt content used by the control-plane projection."""

        payload = {
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_kind": item.artifact_kind,
                    "media_type": item.media_type,
                    "owner_id": item.owner_id,
                    "owner_kind": item.owner_kind,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "storage_key": item.storage_key,
                }
                for item in self.artifacts
            ],
            "committed_at": self.committed_at.isoformat(),
            "evidence_hash": self.evidence_hash,
            "fencing_token": self.fencing_token,
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind,
            "organization_id": str(self.organization_id),
            "project_id": self.project_id,
            "receipt_id": self.receipt_id,
            "semantic_input_hash": self.semantic_input_hash,
            "workspace_id": self.workspace_id,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + sha256(encoded).hexdigest()

    def require_operation(self, request: OperationRequest) -> None:
        """Reject data evidence for another operation or semantic request."""

        if (
            self.operation_id != request.operation_id
            or self.organization_id != request.organization_id
            or self.project_id != request.project_id
            or self.operation_kind != request.operation_kind
            or self.semantic_input_hash != request.semantic_input_hash
        ):
            raise ReceiptMismatchError(
                "data-plane receipt does not match the reserved operation"
            )

    def require_lease(self, lease: WorkspaceLease) -> None:
        """Reject evidence committed without the exact active Workspace lease."""

        fencing_token = self.fencing_token
        if (
            self.workspace_id != lease.workspace_id
            or self.organization_id != lease.organization_id
            or self.project_id != lease.project_id
            or fencing_token is None
            or fencing_token != lease.fencing_token
        ):
            raise ReceiptMismatchError(
                "data-plane receipt does not match the Workspace lease"
            )
        lease.require_fencing_token(
            fencing_token,
            at=self.committed_at,
        )


@dataclass(frozen=True, slots=True)
class ControlPlaneProjectionReceipt:
    """Proof that PostgreSQL projected one exact data-plane receipt."""

    projection_receipt_id: str
    operation_id: str
    organization_id: OrganizationId
    project_id: str
    data_plane_receipt_hash: str
    projection_hash: str
    projection_revision: int
    projected_at: datetime

    def __post_init__(self) -> None:
        for name in ("projection_receipt_id", "operation_id", "project_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=300),
            )
        _sha256_hash(self.data_plane_receipt_hash, "data_plane_receipt_hash")
        _sha256_hash(self.projection_hash, "projection_hash")
        if self.projection_revision < 1:
            raise ValueError("projection_revision must be positive")
        _aware(self.projected_at, "projected_at")

    @classmethod
    def for_data_plane(
        cls,
        receipt: DataPlaneOperationReceipt,
        *,
        projection_receipt_id: str,
        projection_hash: str,
        projection_revision: int,
        projected_at: datetime,
    ) -> ControlPlaneProjectionReceipt:
        """Bind a control-plane projection to the exact committed receipt."""

        return cls(
            projection_receipt_id=projection_receipt_id,
            operation_id=receipt.operation_id,
            organization_id=receipt.organization_id,
            project_id=receipt.project_id,
            data_plane_receipt_hash=receipt.semantic_hash,
            projection_hash=projection_hash,
            projection_revision=projection_revision,
            projected_at=projected_at,
        )

    def require_data_plane_receipt(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> None:
        """Reject a substituted or unrelated data-plane receipt."""

        if (
            receipt.operation_id != self.operation_id
            or receipt.organization_id != self.organization_id
            or receipt.project_id != self.project_id
            or receipt.semantic_hash != self.data_plane_receipt_hash
        ):
            raise ReceiptMismatchError(
                "control-plane projection does not match the data-plane receipt"
            )


@dataclass(frozen=True, slots=True)
class CrossStoreOperationSnapshot:
    """Exact durable operation state and its immutable receipt evidence."""

    request: OperationRequest
    state: CrossStoreOperationState
    data_plane_receipt: DataPlaneOperationReceipt | None = None
    projection_receipt: ControlPlaneProjectionReceipt | None = None
    interrupted_at: datetime | None = None
    recovery_disposition: RecoveryDisposition | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", CrossStoreOperationState(self.state))
        if self.data_plane_receipt is not None:
            self.data_plane_receipt.require_operation(self.request)
        if self.projection_receipt is not None:
            if self.data_plane_receipt is None:
                raise ValueError("projection receipt requires data-plane evidence")
            self.projection_receipt.require_data_plane_receipt(self.data_plane_receipt)
        has_data = self.data_plane_receipt is not None
        has_projection = self.projection_receipt is not None
        if self.state is CrossStoreOperationState.RESERVED and (
            has_data or has_projection
        ):
            raise ValueError("reserved operation cannot contain receipts")
        if self.state is CrossStoreOperationState.DATA_COMMITTED and (
            not has_data or has_projection
        ):
            raise ValueError("data-committed operation requires one data receipt")
        if self.state is CrossStoreOperationState.PROJECTED and not (
            has_data and has_projection
        ):
            raise ValueError("projected operation requires both receipts")
        terminal = self.state in {
            CrossStoreOperationState.INTERRUPTED,
            CrossStoreOperationState.MANUAL_REVIEW,
        }
        if terminal:
            if self.interrupted_at is None or self.recovery_disposition is None:
                raise ValueError("terminal recovery state requires its disposition")
            _aware(self.interrupted_at, "interrupted_at")
            object.__setattr__(
                self,
                "recovery_disposition",
                RecoveryDisposition(self.recovery_disposition),
            )
        elif self.interrupted_at is not None or self.recovery_disposition is not None:
            raise ValueError("non-terminal operation cannot contain recovery state")


@dataclass(frozen=True, slots=True)
class OptimisticMutation:
    """Expected revision supplied by one remote editor."""

    aggregate_kind: str
    aggregate_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        for name in ("aggregate_kind", "aggregate_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=200),
            )
        if self.expected_revision < 1:
            raise ValueError("expected_revision must be positive")


@dataclass(frozen=True, slots=True)
class OptimisticConflict:
    """Safe remote-only conflict result without another user's values."""

    aggregate_kind: str
    aggregate_id: str
    expected_revision: int
    current_revision: int
    conflict_code: str = "STALE_REVISION"

    def __post_init__(self) -> None:
        for name in ("aggregate_kind", "aggregate_id", "conflict_code"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name, maximum=200),
            )
        if self.expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if self.current_revision <= self.expected_revision:
            raise ValueError("current_revision must be newer than expected_revision")


def _required_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


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
