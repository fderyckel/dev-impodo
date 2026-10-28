"""Verify authorized bounded scans and receipt-only projection rebuilds."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from impodo.application.shared.cross_store_coordinator import CrossStoreOperationResult
from impodo.application.shared.projection_reconciliation import (
    ProjectionReconciliationService,
)
from impodo.domain.shared.access import ActorIdentity, AuthorizationError, Capability
from impodo.domain.shared.coordination import (
    ControlPlaneProjectionReceipt,
    CrossStoreOperationSnapshot,
    CrossStoreOperationState,
    DataPlaneOperationReceipt,
    OperationNotFoundError,
    OperationRequest,
)
from impodo.domain.shared.projection_reconciliation import (
    ProjectionRebuildNotPermittedError,
    ProjectionReconciliationDisposition,
    ProjectionReconciliationItem,
    ProjectionReconciliationNotFoundError,
    ProjectionReconciliationPage,
    ProjectionReconciliationQuery,
)
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    ProjectVisibilityScope,
    RequestOrganizationScope,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION = OrganizationId("fictional-organization")
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
OPERATION_ID = "20000000-0000-4000-8000-000000000001"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _request() -> OperationRequest:
    return OperationRequest(
        operation_id=OPERATION_ID,
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


def _data_receipt() -> DataPlaneOperationReceipt:
    return DataPlaneOperationReceipt(
        receipt_id="data-receipt-001",
        operation_id=OPERATION_ID,
        organization_id=ORGANIZATION,
        project_id=PROJECT_ID,
        workspace_id=None,
        operation_kind="workspace.prepare",
        semantic_input_hash=HASH_A,
        fencing_token=None,
        evidence_hash=HASH_B,
        artifacts=(),
        committed_at=NOW + timedelta(minutes=1),
    )


def _projection() -> ControlPlaneProjectionReceipt:
    return ControlPlaneProjectionReceipt.for_data_plane(
        _data_receipt(),
        projection_receipt_id="projection-001",
        projection_hash=HASH_B,
        projection_revision=1,
        projected_at=NOW + timedelta(minutes=1),
    )


def _authorization(*capabilities: Capability) -> AuthorizationReceipt:
    identity = _request().requested_by
    return AuthorizationReceipt(
        receipt_id="authorization-admin-001",
        principal_id="principal-001",
        actor_identity=identity,
        organization_id=ORGANIZATION,
        capabilities=frozenset(capabilities),
        project_visibility=ProjectVisibilityScope(
            organization_id=ORGANIZATION,
            project_ids=frozenset({PROJECT_ID}),
        ),
        role_revision_ids=("role-001",),
        grant_revision_ids=("grant-001",),
        policy_revision="policy-001",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


class _Repository:
    def __init__(self, snapshot: CrossStoreOperationSnapshot) -> None:
        self.snapshot = snapshot
        self.list_calls = 0
        self.get_calls = 0
        self.page = ProjectionReconciliationPage(
            items=(
                ProjectionReconciliationItem(
                    operation_id=OPERATION_ID,
                    operation_kind="workspace.prepare",
                    requested_at=NOW,
                    state=snapshot.state,
                    disposition=ProjectionReconciliationDisposition.MISSING_PROJECTION,
                    data_plane_receipt_hash=_data_receipt().semantic_hash,
                    projection_hash=None,
                    projection_revision=None,
                ),
            ),
            next_cursor="next-page",
        )

    def list_reconciliation_page(
        self,
        organization_id: OrganizationId,
        project_id: str,
        query: ProjectionReconciliationQuery,
    ) -> ProjectionReconciliationPage:
        self.list_calls += 1
        if organization_id != ORGANIZATION or project_id != PROJECT_ID:
            raise AssertionError("repository received an unverified scope")
        if query.page_size != 20:
            raise AssertionError("repository received another query")
        return self.page

    def get_reconciliation_snapshot(
        self,
        organization_id: OrganizationId,
        project_id: str,
        operation_id: str,
    ) -> CrossStoreOperationSnapshot:
        self.get_calls += 1
        if (
            organization_id != ORGANIZATION
            or project_id != PROJECT_ID
            or operation_id != OPERATION_ID
        ):
            raise OperationNotFoundError("Operation not found")
        return self.snapshot


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[OperationRequest] = []

    def recover(self, request: OperationRequest) -> CrossStoreOperationResult:
        self.calls.append(request)
        return CrossStoreOperationResult(
            request=request,
            data_plane_receipt=_data_receipt(),
            projection_receipt=_projection(),
            replayed=True,
        )


class ProjectionReconciliationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        snapshot = CrossStoreOperationSnapshot(
            request=_request(),
            state=CrossStoreOperationState.DATA_COMMITTED,
            data_plane_receipt=_data_receipt(),
        )
        self.repository = _Repository(snapshot)
        self.coordinator = _Coordinator()
        self.service = ProjectionReconciliationService(
            self.repository,
            self.coordinator,
            clock=lambda: NOW,
        )
        self.scope = RequestOrganizationScope(
            organization_id=ORGANIZATION,
            principal_id="principal-001",
        )

    def test_audit_scan_is_authorized_before_one_bounded_repository_page(self) -> None:
        page = self.service.scan(
            PROJECT_ID,
            ProjectionReconciliationQuery(page_size=20),
            scope=self.scope,
            authorization=_authorization(Capability.AUDIT_VIEW),
        )

        self.assertEqual(page, self.repository.page)
        self.assertEqual(self.repository.list_calls, 1)

    def test_invisible_scan_is_opaque_and_never_opens_the_repository(self) -> None:
        invisible = replace(
            _authorization(Capability.AUDIT_VIEW),
            project_visibility=ProjectVisibilityScope(
                organization_id=ORGANIZATION,
            ),
        )

        with self.assertRaisesRegex(
            ProjectionReconciliationNotFoundError,
            "Project not found",
        ):
            self.service.scan(
                PROJECT_ID,
                ProjectionReconciliationQuery(page_size=20),
                scope=self.scope,
                authorization=invisible,
            )

        self.assertEqual(self.repository.list_calls, 0)

    def test_project_admin_rebuilds_only_the_missing_projection(self) -> None:
        result = self.service.rebuild_missing_projection(
            PROJECT_ID,
            OPERATION_ID,
            scope=self.scope,
            authorization=_authorization(Capability.PROJECT_ADMIN),
        )

        self.assertTrue(result.replayed)
        self.assertEqual(self.repository.get_calls, 1)
        self.assertEqual(self.coordinator.calls, [_request()])

    def test_rebuild_refuses_projected_or_unknown_outcomes(self) -> None:
        self.repository.snapshot = CrossStoreOperationSnapshot(
            request=_request(),
            state=CrossStoreOperationState.PROJECTED,
            data_plane_receipt=_data_receipt(),
            projection_receipt=_projection(),
        )

        with self.assertRaises(ProjectionRebuildNotPermittedError):
            self.service.rebuild_missing_projection(
                PROJECT_ID,
                OPERATION_ID,
                scope=self.scope,
                authorization=_authorization(Capability.PROJECT_ADMIN),
            )

        self.assertEqual(self.coordinator.calls, [])

    def test_rebuild_requires_exact_admin_capability_before_storage(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.service.rebuild_missing_projection(
                PROJECT_ID,
                OPERATION_ID,
                scope=self.scope,
                authorization=_authorization(Capability.AUDIT_VIEW),
            )

        self.assertEqual(self.repository.get_calls, 0)


if __name__ == "__main__":
    unittest.main()
