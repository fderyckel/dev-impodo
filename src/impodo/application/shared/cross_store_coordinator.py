"""Finalize cross-store commands only from exact durable receipt evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from impodo.application.shared.coordination import (
    OperationJournal,
    WorkspaceLeaseRepository,
)
from impodo.domain.shared.coordination import (
    ArtifactIdentity,
    ControlPlaneProjectionReceipt,
    CrossStoreOperationSnapshot,
    CrossStoreOperationState,
    DataPlaneOperationReceipt,
    OperationRequest,
    ReceiptMismatchError,
    RecoveryDisposition,
    WorkspaceLease,
)


class CrossStoreBoundary(StrEnum):
    """Named durable boundaries used by failure-injection contract tests."""

    BEFORE_LEASE = "BEFORE_LEASE"
    AFTER_LEASE = "AFTER_LEASE"
    AFTER_ARTIFACT_STAGING = "AFTER_ARTIFACT_STAGING"
    AFTER_DATA_PLANE_COMMIT = "AFTER_DATA_PLANE_COMMIT"
    AFTER_ARTIFACT_PUBLICATION = "AFTER_ARTIFACT_PUBLICATION"
    BEFORE_PROJECTION_UPDATE = "BEFORE_PROJECTION_UPDATE"
    AFTER_PROJECTION_UPDATE = "AFTER_PROJECTION_UPDATE"


class ArtifactEvidenceMismatchError(RuntimeError):
    """Report that a published artifact does not match its receipt identity."""


class CrossStoreOperationInterruptedError(RuntimeError):
    """Report a durable interruption with no proven data-plane commit."""


class CrossStoreManualReviewError(RuntimeError):
    """Stop automatic recovery when durable evidence is ambiguous."""


class BoundaryCheckpoint(Protocol):
    """Observe one named boundary without receiving protected values."""

    def __call__(self, boundary: CrossStoreBoundary) -> None: ...


class DataPlaneCommand(Protocol):
    """Commit one fenced command and recover its immutable receipt by identity."""

    def execute(
        self,
        request: OperationRequest,
        lease: WorkspaceLease,
        checkpoint: BoundaryCheckpoint,
    ) -> DataPlaneOperationReceipt:
        """Commit once and report staging, commit, and publication boundaries."""
        ...

    def find_receipt(
        self,
        request: OperationRequest,
    ) -> DataPlaneOperationReceipt | None:
        """Return committed evidence without repeating the command."""
        ...


class ArtifactEvidenceVerifier(Protocol):
    """Verify one immutable artifact's key, hash, and size."""

    def verify(self, artifact: ArtifactIdentity) -> None:
        """Raise only ``ArtifactEvidenceMismatchError`` for disproven evidence."""
        ...


class ControlPlaneProjectionUpdater(Protocol):
    """Idempotently project one exact data-plane receipt."""

    def project(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> ControlPlaneProjectionReceipt:
        """Create or return the projection for the same receipt."""
        ...


def _ignore_boundary(boundary: CrossStoreBoundary) -> None:
    del boundary


@dataclass(frozen=True, slots=True)
class WorkspaceCrossStoreCommand:
    """Bind one semantic request to its Workspace writer attempt."""

    request: OperationRequest
    workspace_id: str
    holder_attempt_id: str

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must not be blank")
        if not self.holder_attempt_id.strip():
            raise ValueError("holder_attempt_id must not be blank")


@dataclass(frozen=True, slots=True)
class CrossStoreOperationResult:
    """Return the exact data and projection evidence for one success."""

    request: OperationRequest
    data_plane_receipt: DataPlaneOperationReceipt
    projection_receipt: ControlPlaneProjectionReceipt
    replayed: bool

    def __post_init__(self) -> None:
        self.data_plane_receipt.require_operation(self.request)
        self.projection_receipt.require_data_plane_receipt(self.data_plane_receipt)


class CrossStoreOperationCoordinator:
    """Coordinate one leased mutation without sharing a database transaction."""

    def __init__(
        self,
        *,
        journal: OperationJournal,
        leases: WorkspaceLeaseRepository,
        data_plane: DataPlaneCommand,
        artifacts: ArtifactEvidenceVerifier,
        projections: ControlPlaneProjectionUpdater,
        checkpoint: BoundaryCheckpoint = _ignore_boundary,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._journal = journal
        self._leases = leases
        self._data_plane = data_plane
        self._artifacts = artifacts
        self._projections = projections
        self._checkpoint = checkpoint
        self._clock = clock
        self._lease_duration = lease_duration

    def execute(
        self,
        command: WorkspaceCrossStoreCommand,
    ) -> CrossStoreOperationResult:
        """Execute a new reservation or recover a replay without repeating work."""

        reservation = self._journal.reserve_with_status(command.request)
        if not reservation.created:
            return self.recover(reservation.request)

        self._checkpoint(CrossStoreBoundary.BEFORE_LEASE)
        acquired_at = self._now()
        lease = self._leases.acquire(
            organization_id=reservation.request.organization_id,
            project_id=reservation.request.project_id,
            workspace_id=command.workspace_id,
            holder_attempt_id=command.holder_attempt_id,
            acquired_at=acquired_at,
            expires_at=acquired_at + self._lease_duration,
        )
        self._checkpoint(CrossStoreBoundary.AFTER_LEASE)
        try:
            receipt = self._data_plane.execute(
                reservation.request,
                lease,
                self._checkpoint,
            )
            receipt.require_operation(reservation.request)
            receipt.require_lease(lease)
            result = self._finalize(
                reservation.request,
                receipt,
                replayed=False,
            )
        except ReceiptMismatchError as error:
            self._manual_review(
                reservation.request,
                RecoveryDisposition.RECEIPT_MISMATCH,
            )
            raise CrossStoreManualReviewError(
                "Cross-store receipt evidence requires manual review"
            ) from error
        self._leases.release(lease, at=self._now())
        return result

    def recover(self, request: OperationRequest) -> CrossStoreOperationResult:
        """Recover from durable state without executing the data-plane command."""

        snapshot = self._journal.get(request)
        if snapshot.state is CrossStoreOperationState.PROJECTED:
            return _result_from_snapshot(snapshot, replayed=True)
        if snapshot.state is CrossStoreOperationState.DATA_COMMITTED:
            receipt = _required_data_receipt(snapshot)
            return self._finalize(request, receipt, replayed=True)
        if snapshot.state is CrossStoreOperationState.RESERVED:
            recovered = self._data_plane.find_receipt(request)
            if recovered is None:
                self._journal.record_interruption(
                    request,
                    interrupted_at=self._now(),
                    disposition=RecoveryDisposition.NO_COMMITTED_RECEIPT,
                )
                raise CrossStoreOperationInterruptedError(
                    "Cross-store operation was interrupted before a proven commit"
                )
            try:
                recovered.require_operation(request)
                self._journal.record_data_plane_receipt(recovered)
            except ReceiptMismatchError as error:
                self._manual_review(
                    request,
                    RecoveryDisposition.RECEIPT_MISMATCH,
                )
                raise CrossStoreManualReviewError(
                    "Recovered receipt evidence requires manual review"
                ) from error
            return self._finalize(request, recovered, replayed=True)
        if snapshot.state is CrossStoreOperationState.INTERRUPTED:
            raise CrossStoreOperationInterruptedError(
                "Cross-store operation has a durable interruption"
            )
        raise CrossStoreManualReviewError(
            "Cross-store operation requires manual review"
        )

    def _finalize(
        self,
        request: OperationRequest,
        receipt: DataPlaneOperationReceipt,
        *,
        replayed: bool,
    ) -> CrossStoreOperationResult:
        receipt.require_operation(request)
        self._journal.record_data_plane_receipt(receipt)
        try:
            for artifact in receipt.artifacts:
                self._artifacts.verify(artifact)
        except ArtifactEvidenceMismatchError as error:
            self._manual_review(
                request,
                RecoveryDisposition.ARTIFACT_MISMATCH,
            )
            raise CrossStoreManualReviewError(
                "Published artifact evidence requires manual review"
            ) from error

        self._checkpoint(CrossStoreBoundary.BEFORE_PROJECTION_UPDATE)
        projection = self._projections.project(receipt)
        try:
            projection.require_data_plane_receipt(receipt)
            self._journal.record_projection_receipt(projection)
        except ReceiptMismatchError as error:
            self._manual_review(
                request,
                RecoveryDisposition.PROJECTION_MISMATCH,
            )
            raise CrossStoreManualReviewError(
                "Control-plane projection requires manual review"
            ) from error
        self._checkpoint(CrossStoreBoundary.AFTER_PROJECTION_UPDATE)
        return CrossStoreOperationResult(
            request=request,
            data_plane_receipt=receipt,
            projection_receipt=projection,
            replayed=replayed,
        )

    def _manual_review(
        self,
        request: OperationRequest,
        disposition: RecoveryDisposition,
    ) -> None:
        self._journal.require_manual_review(
            request,
            interrupted_at=self._now(),
            disposition=disposition,
        )

    def _now(self) -> datetime:
        current = self._clock()
        if current.utcoffset() is None:
            raise ValueError("coordination clock must return an aware timestamp")
        return current


def _required_data_receipt(
    snapshot: CrossStoreOperationSnapshot,
) -> DataPlaneOperationReceipt:
    receipt = snapshot.data_plane_receipt
    if receipt is None:
        raise ReceiptMismatchError("durable operation is missing data-plane evidence")
    return receipt


def _result_from_snapshot(
    snapshot: CrossStoreOperationSnapshot,
    *,
    replayed: bool,
) -> CrossStoreOperationResult:
    data_receipt = _required_data_receipt(snapshot)
    projection = snapshot.projection_receipt
    if projection is None:
        raise ReceiptMismatchError("durable operation is missing projection evidence")
    return CrossStoreOperationResult(
        request=snapshot.request,
        data_plane_receipt=data_receipt,
        projection_receipt=projection,
        replayed=replayed,
    )


__all__ = [
    "ArtifactEvidenceMismatchError",
    "ArtifactEvidenceVerifier",
    "BoundaryCheckpoint",
    "ControlPlaneProjectionUpdater",
    "CrossStoreBoundary",
    "CrossStoreManualReviewError",
    "CrossStoreOperationCoordinator",
    "CrossStoreOperationInterruptedError",
    "CrossStoreOperationResult",
    "DataPlaneCommand",
    "WorkspaceCrossStoreCommand",
]
