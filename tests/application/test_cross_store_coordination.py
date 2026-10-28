"""Verify receipt-driven cross-store execution and deterministic recovery."""

from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from impodo.application.shared.cross_store_coordinator import (
    ArtifactEvidenceMismatchError,
    CrossStoreBoundary,
    CrossStoreManualReviewError,
    CrossStoreOperationCoordinator,
    CrossStoreOperationInterruptedError,
    WorkspaceCrossStoreCommand,
)
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.coordination import (
    ArtifactIdentity,
    ControlPlaneProjectionReceipt,
    CrossStoreOperationSnapshot,
    CrossStoreOperationState,
    DataPlaneOperationReceipt,
    IdempotencyConflictError,
    OperationRequest,
    OperationReservation,
    ReceiptMismatchError,
    RecoveryDisposition,
    WorkspaceLease,
)
from impodo.domain.shared.tenancy import OrganizationId

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION = OrganizationId("fictional-organization")
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
OPERATION_ID = "20000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "30000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "40000000-0000-4000-8000-000000000001"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _request(*, operation_id: str = OPERATION_ID) -> OperationRequest:
    return OperationRequest(
        operation_id=operation_id,
        organization_id=ORGANIZATION,
        project_id=PROJECT_ID,
        operation_kind="workspace.prepare",
        idempotency_key="prepare:workspace:revision-1",
        semantic_input_hash=HASH_A,
        authorization_receipt_id="authorization-001",
        requested_by=ActorIdentity(
            issuer="urn:impodo:test",
            subject_id="fictional-user",
            display_name="Fictional user",
        ),
        requested_at=NOW,
    )


def _receipt() -> DataPlaneOperationReceipt:
    return DataPlaneOperationReceipt(
        receipt_id="data-receipt-001",
        operation_id=OPERATION_ID,
        organization_id=ORGANIZATION,
        project_id=PROJECT_ID,
        workspace_id=WORKSPACE_ID,
        operation_kind="workspace.prepare",
        semantic_input_hash=HASH_A,
        fencing_token=1,
        evidence_hash=HASH_B,
        artifacts=(
            ArtifactIdentity(
                artifact_id="artifact-001",
                owner_kind="workspace",
                owner_id=WORKSPACE_ID,
                artifact_kind="prepared_snapshot",
                storage_key="organizations/fictional/prepared.parquet",
                sha256=HASH_C,
                size_bytes=128,
                media_type="application/vnd.apache.parquet",
            ),
        ),
        committed_at=NOW + timedelta(minutes=1),
    )


class _Journal:
    def __init__(self) -> None:
        self.requests: dict[tuple[OrganizationId, str], OperationRequest] = {}
        self.snapshots: dict[str, CrossStoreOperationSnapshot] = {}

    def reserve(self, request: OperationRequest) -> OperationRequest:
        return self.reserve_with_status(request).request

    def reserve_with_status(
        self,
        request: OperationRequest,
    ) -> OperationReservation:
        existing = self.requests.get(request.idempotency_scope)
        if existing is not None:
            existing.require_same_idempotent_request(request)
            return OperationReservation(request=existing, created=False)
        self.requests[request.idempotency_scope] = request
        self.snapshots[request.operation_id] = CrossStoreOperationSnapshot(
            request=request,
            state=CrossStoreOperationState.RESERVED,
        )
        return OperationReservation(request=request, created=True)

    def get(self, request: OperationRequest) -> CrossStoreOperationSnapshot:
        snapshot = self.snapshots[request.operation_id]
        snapshot.request.require_same_idempotent_request(request)
        return snapshot

    def record_data_plane_receipt(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> None:
        snapshot = self.snapshots[receipt.operation_id]
        receipt.require_operation(snapshot.request)
        if snapshot.data_plane_receipt is not None:
            if snapshot.data_plane_receipt != receipt:
                raise ReceiptMismatchError("different data-plane evidence")
            return
        if snapshot.state is not CrossStoreOperationState.RESERVED:
            raise ReceiptMismatchError("operation cannot accept data evidence")
        self.snapshots[receipt.operation_id] = CrossStoreOperationSnapshot(
            request=snapshot.request,
            state=CrossStoreOperationState.DATA_COMMITTED,
            data_plane_receipt=receipt,
        )

    def record_projection_receipt(
        self,
        receipt: ControlPlaneProjectionReceipt,
    ) -> None:
        snapshot = self.snapshots[receipt.operation_id]
        data_receipt = snapshot.data_plane_receipt
        if data_receipt is None:
            raise ReceiptMismatchError("projection has no data receipt")
        receipt.require_data_plane_receipt(data_receipt)
        if snapshot.projection_receipt is not None:
            if snapshot.projection_receipt != receipt:
                raise ReceiptMismatchError("different projection evidence")
            return
        if snapshot.state is not CrossStoreOperationState.DATA_COMMITTED:
            raise ReceiptMismatchError("operation cannot accept projection")
        self.snapshots[receipt.operation_id] = CrossStoreOperationSnapshot(
            request=snapshot.request,
            state=CrossStoreOperationState.PROJECTED,
            data_plane_receipt=data_receipt,
            projection_receipt=receipt,
        )

    def record_interruption(
        self,
        request: OperationRequest,
        *,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        snapshot = self.get(request)
        if snapshot.state is not CrossStoreOperationState.RESERVED:
            raise RuntimeError("only a reservation can be interrupted")
        self.snapshots[request.operation_id] = CrossStoreOperationSnapshot(
            request=request,
            state=CrossStoreOperationState.INTERRUPTED,
            interrupted_at=interrupted_at,
            recovery_disposition=disposition,
        )

    def require_manual_review(
        self,
        request: OperationRequest,
        *,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        snapshot = self.get(request)
        self.snapshots[request.operation_id] = CrossStoreOperationSnapshot(
            request=snapshot.request,
            state=CrossStoreOperationState.MANUAL_REVIEW,
            data_plane_receipt=snapshot.data_plane_receipt,
            interrupted_at=interrupted_at,
            recovery_disposition=disposition,
        )


class _Leases:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(
        self,
        *,
        organization_id: OrganizationId,
        project_id: str,
        workspace_id: str,
        holder_attempt_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        self.acquired += 1
        return WorkspaceLease(
            lease_id=f"lease-{self.acquired}",
            organization_id=organization_id,
            project_id=project_id,
            workspace_id=workspace_id,
            holder_attempt_id=holder_attempt_id,
            fencing_token=self.acquired,
            acquired_at=acquired_at,
            heartbeat_at=acquired_at,
            expires_at=expires_at,
        )

    def heartbeat(
        self,
        lease: WorkspaceLease,
        *,
        at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        return lease.heartbeat(
            fencing_token=lease.fencing_token,
            at=at,
            expires_at=expires_at,
        )

    def release(self, lease: WorkspaceLease, *, at: datetime) -> WorkspaceLease:
        released = lease.release(fencing_token=lease.fencing_token, at=at)
        self.released += 1
        return released


class _DataPlane:
    def __init__(self, receipt: DataPlaneOperationReceipt) -> None:
        self.receipt = receipt
        self.stored_receipt: DataPlaneOperationReceipt | None = None
        self.executions = 0

    def execute(self, request, lease, checkpoint):
        self.executions += 1
        checkpoint(CrossStoreBoundary.AFTER_ARTIFACT_STAGING)
        self.stored_receipt = replace(
            self.receipt,
            operation_id=request.operation_id,
            organization_id=request.organization_id,
            project_id=request.project_id,
            workspace_id=lease.workspace_id,
            operation_kind=request.operation_kind,
            semantic_input_hash=request.semantic_input_hash,
            fencing_token=lease.fencing_token,
        )
        checkpoint(CrossStoreBoundary.AFTER_DATA_PLANE_COMMIT)
        checkpoint(CrossStoreBoundary.AFTER_ARTIFACT_PUBLICATION)
        return self.stored_receipt

    def find_receipt(self, request):
        del request
        return self.stored_receipt


class _Artifacts:
    def __init__(self) -> None:
        self.verified: list[ArtifactIdentity] = []
        self.mismatch = False

    def verify(self, artifact: ArtifactIdentity) -> None:
        if self.mismatch:
            raise ArtifactEvidenceMismatchError("artifact hash mismatch")
        self.verified.append(artifact)


class _Projections:
    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0
        self.substitute = False

    def project(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> ControlPlaneProjectionReceipt:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("projection unavailable")
        projection = ControlPlaneProjectionReceipt.for_data_plane(
            receipt,
            projection_receipt_id="projection-001",
            projection_hash=HASH_C,
            projection_revision=1,
            projected_at=NOW + timedelta(minutes=2),
        )
        return (
            replace(projection, data_plane_receipt_hash=HASH_A)
            if self.substitute
            else projection
        )


class _Fault:
    def __init__(self, boundary: CrossStoreBoundary | None = None) -> None:
        self.boundary = boundary
        self.reached: list[CrossStoreBoundary] = []
        self.raised = False

    def __call__(self, boundary: CrossStoreBoundary) -> None:
        self.reached.append(boundary)
        if boundary is self.boundary and not self.raised:
            self.raised = True
            raise RuntimeError(f"injected failure at {boundary}")


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        observed = self.current
        self.current += timedelta(seconds=1)
        return observed


@dataclass
class _Fixture:
    journal: _Journal
    leases: _Leases
    data_plane: _DataPlane
    artifacts: _Artifacts
    projections: _Projections
    fault: _Fault
    coordinator: CrossStoreOperationCoordinator


def _fixture(
    boundary: CrossStoreBoundary | None = None,
) -> _Fixture:
    journal = _Journal()
    leases = _Leases()
    data_plane = _DataPlane(_receipt())
    artifacts = _Artifacts()
    projections = _Projections()
    fault = _Fault(boundary)
    coordinator = CrossStoreOperationCoordinator(
        journal=journal,
        leases=leases,
        data_plane=data_plane,
        artifacts=artifacts,
        projections=projections,
        checkpoint=fault,
        clock=_Clock(),
    )
    return _Fixture(
        journal=journal,
        leases=leases,
        data_plane=data_plane,
        artifacts=artifacts,
        projections=projections,
        fault=fault,
        coordinator=coordinator,
    )


def _command(request: OperationRequest | None = None) -> WorkspaceCrossStoreCommand:
    return WorkspaceCrossStoreCommand(
        request=request or _request(),
        workspace_id=WORKSPACE_ID,
        holder_attempt_id=ATTEMPT_ID,
    )


class CrossStoreOperationCoordinatorTests(unittest.TestCase):
    def test_success_and_exact_replay_never_repeat_data_plane_work(self) -> None:
        fixture = _fixture()

        created = fixture.coordinator.execute(_command())
        replayed = fixture.coordinator.execute(
            _command(_request(operation_id="20000000-0000-4000-8000-000000000002"))
        )

        self.assertFalse(created.replayed)
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.request.operation_id, OPERATION_ID)
        self.assertEqual(fixture.data_plane.executions, 1)
        self.assertEqual(fixture.projections.calls, 1)
        self.assertEqual(fixture.leases.acquired, 1)
        self.assertEqual(fixture.leases.released, 1)
        self.assertEqual(
            fixture.journal.get(_request()).state,
            CrossStoreOperationState.PROJECTED,
        )

    def test_every_boundary_has_a_deterministic_recovery_result(self) -> None:
        interrupted_boundaries = {
            CrossStoreBoundary.BEFORE_LEASE,
            CrossStoreBoundary.AFTER_LEASE,
            CrossStoreBoundary.AFTER_ARTIFACT_STAGING,
        }
        for boundary in CrossStoreBoundary:
            with self.subTest(boundary=boundary):
                fixture = _fixture(boundary)
                request = _request()

                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    fixture.coordinator.execute(_command(request))

                if boundary in interrupted_boundaries:
                    with self.assertRaises(CrossStoreOperationInterruptedError):
                        fixture.coordinator.recover(request)
                    state = CrossStoreOperationState.INTERRUPTED
                else:
                    result = fixture.coordinator.recover(request)
                    self.assertTrue(result.replayed)
                    state = CrossStoreOperationState.PROJECTED
                self.assertEqual(fixture.journal.get(request).state, state)
                self.assertLessEqual(fixture.data_plane.executions, 1)

    def test_artifact_mismatch_requires_manual_review_before_projection(self) -> None:
        fixture = _fixture()
        fixture.artifacts.mismatch = True

        with self.assertRaises(CrossStoreManualReviewError):
            fixture.coordinator.execute(_command())

        snapshot = fixture.journal.get(_request())
        self.assertEqual(snapshot.state, CrossStoreOperationState.MANUAL_REVIEW)
        self.assertEqual(
            snapshot.recovery_disposition,
            RecoveryDisposition.ARTIFACT_MISMATCH,
        )
        self.assertEqual(fixture.projections.calls, 0)

    def test_substituted_recovered_receipt_requires_manual_review(self) -> None:
        fixture = _fixture()
        request = _request()
        fixture.journal.reserve_with_status(request)
        fixture.data_plane.stored_receipt = replace(
            _receipt(),
            semantic_input_hash=HASH_B,
        )

        with self.assertRaises(CrossStoreManualReviewError):
            fixture.coordinator.recover(request)

        snapshot = fixture.journal.get(request)
        self.assertEqual(snapshot.state, CrossStoreOperationState.MANUAL_REVIEW)
        self.assertEqual(
            snapshot.recovery_disposition,
            RecoveryDisposition.RECEIPT_MISMATCH,
        )

    def test_projection_mismatch_requires_manual_review(self) -> None:
        fixture = _fixture()
        fixture.projections.substitute = True

        with self.assertRaises(CrossStoreManualReviewError):
            fixture.coordinator.execute(_command())

        snapshot = fixture.journal.get(_request())
        self.assertEqual(snapshot.state, CrossStoreOperationState.MANUAL_REVIEW)
        self.assertEqual(
            snapshot.recovery_disposition,
            RecoveryDisposition.PROJECTION_MISMATCH,
        )

    def test_transient_projection_failure_recovers_without_data_reexecution(
        self,
    ) -> None:
        fixture = _fixture()
        fixture.projections.failures = 1

        with self.assertRaisesRegex(RuntimeError, "projection unavailable"):
            fixture.coordinator.execute(_command())
        self.assertEqual(
            fixture.journal.get(_request()).state,
            CrossStoreOperationState.DATA_COMMITTED,
        )

        result = fixture.coordinator.recover(_request())

        self.assertTrue(result.replayed)
        self.assertEqual(fixture.data_plane.executions, 1)
        self.assertEqual(fixture.projections.calls, 2)
        self.assertEqual(
            fixture.journal.get(_request()).state,
            CrossStoreOperationState.PROJECTED,
        )

    def test_changed_meaning_under_same_key_fails_before_lease(self) -> None:
        fixture = _fixture()
        fixture.coordinator.execute(_command())
        changed = replace(
            _request(operation_id="20000000-0000-4000-8000-000000000002"),
            semantic_input_hash=HASH_B,
        )

        with self.assertRaises(IdempotencyConflictError):
            fixture.coordinator.execute(_command(changed))

        self.assertEqual(fixture.data_plane.executions, 1)
        self.assertEqual(fixture.leases.acquired, 1)


if __name__ == "__main__":
    unittest.main()
