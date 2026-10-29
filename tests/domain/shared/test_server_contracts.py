"""Contract tests for the future shared-server control/data boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import unittest

from impodo.application.project.ports import ScopedMigrationProjectQuery
from impodo.domain.project.models import (
    MigrationProject,
    MigrationProjectStatus,
    MigrationProjectSummary,
)
from impodo.domain.shared.access import ActorIdentity, Capability
from impodo.domain.shared.coordination import (
    ArtifactIdentity,
    ControlPlaneProjectionReceipt,
    DataPlaneOperationReceipt,
    OperationRequest,
    OptimisticConflict,
    OptimisticMutation,
    ReceiptMismatchError,
    StaleLeaseError,
    WorkspaceLease,
)
from impodo.domain.shared.jobs import (
    DurableJobAttempt,
    DurableJobKind,
    DurableJobRequest,
    DurableJobState,
    JobAttemptIdentity,
    JobCancellation,
    JobCancellationDisposition,
    JobCancellationReceipt,
    JobInterruption,
    JobProgress,
    JobReceiptCheck,
    JobReceiptCheckStatus,
    JobRecoveryDecision,
    JobRecoveryDisposition,
    JobResultIdentity,
    JobStateTransition,
)
from impodo.domain.shared.retention import (
    DEFAULT_INACTIVITY_RETENTION_DAYS,
    DeletionResultStatus,
    MinimalDeletionReceipt,
    ProjectDeletionRequest,
    ProjectDeletionResult,
    ProjectRetentionSelection,
    RetentionMode,
)
from impodo.domain.shared.tenancy import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    AuthorizationReceipt,
    OrganizationId,
    PrincipalKind,
    ProjectVisibilityScope,
    RequestOrganizationScope,
)


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def identity(name: str = "Data manager") -> ActorIdentity:
    return ActorIdentity(
        issuer="urn:impodo:account",
        subject_id=name.casefold().replace(" ", "-"),
        display_name=name,
    )


class InMemoryOperationReservations:
    """Contract fake proving Organization-scoped semantic idempotency."""

    def __init__(self) -> None:
        self._requests: dict[tuple[OrganizationId, str], OperationRequest] = {}

    def reserve(self, request: OperationRequest) -> OperationRequest:
        existing = self._requests.get(request.idempotency_scope)
        if existing is not None:
            existing.require_same_idempotent_request(request)
            return existing
        self._requests[request.idempotency_scope] = request
        return request


class InMemoryScopedProjectQuery(ScopedMigrationProjectQuery):
    """Contract fake that filters before returning any Project metadata."""

    def __init__(self, summaries: tuple[MigrationProjectSummary, ...]) -> None:
        self._summaries = summaries

    def list_visible_project_summaries(
        self,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> tuple[MigrationProjectSummary, ...]:
        if authorization.organization_id != scope.organization_id:
            raise PermissionError("authorization scope does not match request")
        return tuple(
            summary
            for summary in self._summaries
            if authorization.project_visibility.allows(summary.project_id)
        )

    def get_visible_project(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        capability: Capability = Capability.PROJECT_VIEW,
    ) -> MigrationProject:
        del project_id, scope, authorization, capability
        raise LookupError("Project not found")


class TenancyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.organization = OrganizationId("organization-001")

    def principal(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            principal_id="user-001",
            actor_identity=identity(),
            kind=PrincipalKind.CUSTOMER,
            authentication_method=AuthenticationMethod.IMPODO_PASSWORD,
            authenticated_at=NOW,
            organization_id=self.organization,
            session_id="session-001",
        )

    def test_customer_principal_belongs_to_exactly_one_organization(self) -> None:
        principal = self.principal()
        scope = RequestOrganizationScope.for_principal(principal)

        self.assertEqual(scope.organization_id, self.organization)
        with self.assertRaisesRegex(ValueError, "another Organization"):
            RequestOrganizationScope.for_principal(
                principal,
                organization_id=OrganizationId("organization-002"),
            )
        with self.assertRaisesRegex(ValueError, "must belong to one Organization"):
            replace(principal, organization_id=None)

    def test_platform_principal_requires_explicit_request_scope(self) -> None:
        principal = AuthenticatedPrincipal(
            principal_id="platform-001",
            actor_identity=identity("Platform operator"),
            kind=PrincipalKind.PLATFORM,
            authentication_method=AuthenticationMethod.IMPODO_PASSWORD,
            authenticated_at=NOW,
            organization_id=None,
            session_id="session-platform",
        )

        with self.assertRaisesRegex(ValueError, "explicit Organization"):
            RequestOrganizationScope.for_principal(principal)
        selected = RequestOrganizationScope.for_principal(
            principal,
            organization_id=self.organization,
        )
        self.assertEqual(selected.organization_id, self.organization)

    def test_authorization_binds_capability_visibility_revision_and_expiry(
        self,
    ) -> None:
        visibility = ProjectVisibilityScope(
            organization_id=self.organization,
            project_ids=frozenset({"project-001"}),
        )
        receipt = AuthorizationReceipt(
            receipt_id="authorization-001",
            principal_id="user-001",
            actor_identity=identity(),
            organization_id=self.organization,
            capabilities=frozenset({Capability.MAPPING_EDIT}),
            project_visibility=visibility,
            role_revision_ids=("role-revision-002", "role-revision-001"),
            grant_revision_ids=("grant-revision-001",),
            policy_revision="policy-001",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

        self.assertTrue(
            receipt.authorizes(
                Capability.MAPPING_EDIT,
                project_id="project-001",
                at=NOW + timedelta(minutes=30),
            )
        )
        self.assertFalse(
            receipt.authorizes(
                Capability.MAPPING_EDIT,
                project_id="project-002",
                at=NOW,
            )
        )
        self.assertFalse(
            receipt.authorizes(
                Capability.MAPPING_EDIT,
                project_id="project-001",
                at=NOW + timedelta(hours=1),
            )
        )
        self.assertEqual(
            receipt.role_revision_ids,
            ("role-revision-001", "role-revision-002"),
        )
        with self.assertRaises(FrozenInstanceError):
            receipt.policy_revision = "changed"  # type: ignore[misc]

    def test_project_query_returns_only_visible_summaries(self) -> None:
        summaries = tuple(
            MigrationProjectSummary(
                project_id=f"project-00{index}",
                display_name=f"Project {index}",
                status=MigrationProjectStatus.ACTIVE,
                optimistic_revision=1,
                data_version_count=0,
                run_count=0,
                workspace_count=0,
                recipe_count=0,
                updated_at=NOW,
            )
            for index in (1, 2)
        )
        query = InMemoryScopedProjectQuery(summaries)
        principal = self.principal()
        scope = RequestOrganizationScope.for_principal(principal)
        receipt = AuthorizationReceipt(
            receipt_id="authorization-visibility",
            principal_id=principal.principal_id,
            actor_identity=principal.actor_identity,
            organization_id=self.organization,
            capabilities=frozenset({Capability.PROJECT_VIEW}),
            project_visibility=ProjectVisibilityScope(
                organization_id=self.organization,
                project_ids=frozenset({"project-002"}),
            ),
            role_revision_ids=("role-revision-001",),
            grant_revision_ids=("grant-revision-001",),
            policy_revision="policy-001",
            issued_at=NOW,
        )

        visible = query.list_visible_project_summaries(
            scope=scope,
            authorization=receipt,
        )

        self.assertEqual(
            tuple(summary.project_id for summary in visible),
            ("project-002",),
        )


class OperationAndLeaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.organization = OrganizationId("organization-001")
        self.operation = OperationRequest(
            operation_id="operation-001",
            organization_id=self.organization,
            project_id="project-001",
            operation_kind="workspace.mapping.save",
            idempotency_key="save:workspace-001:revision-7",
            semantic_input_hash=HASH_A,
            authorization_receipt_id="authorization-001",
            requested_by=identity(),
            requested_at=NOW,
        )

    def lease(self, *, token: int = 7) -> WorkspaceLease:
        return WorkspaceLease(
            lease_id=f"lease-{token}",
            organization_id=self.organization,
            project_id="project-001",
            workspace_id="workspace-001",
            holder_attempt_id=f"attempt-{token}",
            fencing_token=token,
            acquired_at=NOW,
            heartbeat_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    def test_idempotency_reuses_only_same_semantic_request(self) -> None:
        reservations = InMemoryOperationReservations()

        self.assertIs(reservations.reserve(self.operation), self.operation)
        duplicate = replace(self.operation, operation_id="operation-retry")
        self.assertIs(reservations.reserve(duplicate), self.operation)
        with self.assertRaisesRegex(ValueError, "different semantic input"):
            reservations.reserve(
                replace(
                    self.operation,
                    operation_id="operation-conflict",
                    semantic_input_hash=HASH_B,
                )
            )

    def test_lease_heartbeat_release_and_stale_fencing_are_explicit(self) -> None:
        lease = self.lease()
        heartbeat = lease.heartbeat(
            fencing_token=7,
            at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=8),
        )
        self.assertEqual(heartbeat.version, 2)

        with self.assertRaises(StaleLeaseError):
            heartbeat.require_fencing_token(6, at=NOW + timedelta(minutes=2))
        released = heartbeat.release(
            fencing_token=7,
            at=NOW + timedelta(minutes=2),
        )
        with self.assertRaises(StaleLeaseError):
            released.require_fencing_token(7, at=NOW + timedelta(minutes=3))

        successor = self.lease(token=8)
        self.assertTrue(successor.supersedes(lease))
        self.assertFalse(lease.supersedes(successor))


class ReceiptAndConflictContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.organization = OrganizationId("organization-001")
        self.artifact = ArtifactIdentity(
            artifact_id="artifact-001",
            owner_kind="workspace",
            owner_id="workspace-001",
            artifact_kind="prepared_snapshot",
            storage_key="organizations/001/projects/001/prepared.parquet",
            sha256=HASH_B,
            size_bytes=1_024,
            media_type="application/vnd.apache.parquet",
        )
        self.receipt = DataPlaneOperationReceipt(
            receipt_id="receipt-001",
            operation_id="operation-001",
            organization_id=self.organization,
            project_id="project-001",
            workspace_id="workspace-001",
            operation_kind="workspace.prepare",
            semantic_input_hash=HASH_A,
            fencing_token=7,
            evidence_hash=HASH_C,
            artifacts=(self.artifact,),
            committed_at=NOW,
        )

    def test_projection_rejects_substituted_data_plane_receipt(self) -> None:
        projection = ControlPlaneProjectionReceipt.for_data_plane(
            self.receipt,
            projection_receipt_id="projection-001",
            projection_hash=HASH_A,
            projection_revision=1,
            projected_at=NOW + timedelta(seconds=1),
        )

        projection.require_data_plane_receipt(self.receipt)
        substituted = replace(self.receipt, evidence_hash=HASH_B)
        with self.assertRaises(ReceiptMismatchError):
            projection.require_data_plane_receipt(substituted)

    def test_artifact_identity_rejects_filesystem_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "portable key"):
            replace(self.artifact, storage_key="../customer.csv")

    def test_optimistic_conflict_exposes_revisions_not_customer_values(self) -> None:
        mutation = OptimisticMutation(
            aggregate_kind="mapping",
            aggregate_id="mapping-001",
            expected_revision=4,
        )
        conflict = OptimisticConflict(
            aggregate_kind=mutation.aggregate_kind,
            aggregate_id=mutation.aggregate_id,
            expected_revision=mutation.expected_revision,
            current_revision=5,
        )

        self.assertEqual(conflict.conflict_code, "STALE_REVISION")
        self.assertFalse(hasattr(conflict, "current_values"))


class DurableJobContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.attempt = JobAttemptIdentity(
            attempt_id="attempt-001",
            job_id="job-001",
            attempt_number=1,
            fencing_token=7,
        )

    def test_job_contract_covers_progress_cancel_interruption_and_result(self) -> None:
        request = DurableJobRequest(
            job_id="job-001",
            operation_id="operation-001",
            organization_id=OrganizationId("organization-001"),
            project_id="project-001",
            workspace_id="workspace-001",
            job_kind=DurableJobKind.PREPARATION,
            idempotency_key="prepare:workspace-001:revision-7",
            semantic_input_hash=HASH_A,
            manifest_hash=HASH_B,
            authorization_receipt_id="authorization-001",
            authorization_revision=3,
            requested_by=identity(),
            requested_at=NOW,
        )
        started = JobStateTransition(
            transition_id="transition-001",
            attempt=self.attempt,
            from_state=DurableJobState.QUEUED,
            to_state=DurableJobState.RUNNING,
            expected_revision=1,
            resulting_revision=2,
            occurred_at=NOW,
        )
        attempt = DurableJobAttempt(
            identity=self.attempt,
            worker_id="worker-001",
            started_at=NOW,
            heartbeat_at=NOW + timedelta(seconds=5),
        )
        progress = JobProgress(
            attempt=self.attempt,
            phase_code="PREPARING_ROWS",
            completed_units=5,
            total_units=10,
            updated_at=NOW,
        )
        cancellation = JobCancellation(
            cancellation_id="cancellation-001",
            attempt=self.attempt,
            requested_by=identity("Reviewer"),
            requested_at=NOW,
            reason_code="USER_REQUESTED",
        )
        cancellation_receipt = JobCancellationReceipt(
            cancellation_id=cancellation.cancellation_id,
            attempt=self.attempt,
            disposition=JobCancellationDisposition.ACCEPTED,
            disposed_at=NOW + timedelta(seconds=1),
        )
        receipt_check = JobReceiptCheck(
            check_id="receipt-check-001",
            attempt=self.attempt,
            operation_id=request.operation_id,
            status=JobReceiptCheckStatus.NO_COMMITTED_RECEIPT,
            attempt_expired_at=NOW + timedelta(seconds=10),
            checked_at=NOW + timedelta(seconds=11),
        )
        recovery_decision = JobRecoveryDecision(
            decision_id="recovery-decision-001",
            receipt_check=receipt_check,
            disposition=JobRecoveryDisposition.INTERRUPT_RETRYABLE,
            decided_at=NOW + timedelta(seconds=12),
            reason_code="NO_COMMITTED_RECEIPT",
        )
        interruption = JobInterruption(
            interruption_id="interruption-001",
            attempt=self.attempt,
            recovery_decision=recovery_decision,
            interrupted_at=NOW + timedelta(seconds=13),
            reason_code="WORKER_RESTARTED",
        )
        result = JobResultIdentity(
            result_id="result-001",
            attempt=self.attempt,
            data_plane_receipt_hash=HASH_B,
            result_artifact_ids=("artifact-002", "artifact-001"),
            completed_at=NOW,
        )

        self.assertEqual(request.organization_id, OrganizationId("organization-001"))
        self.assertIs(request.job_kind, DurableJobKind.PREPARATION)
        self.assertEqual(started.to_state, DurableJobState.RUNNING)
        self.assertEqual(attempt.worker_id, "worker-001")
        self.assertEqual(progress.total_units, 10)
        self.assertEqual(cancellation.reason_code, "USER_REQUESTED")
        self.assertIs(
            cancellation_receipt.disposition,
            JobCancellationDisposition.ACCEPTED,
        )
        self.assertIs(
            recovery_decision.disposition,
            JobRecoveryDisposition.INTERRUPT_RETRYABLE,
        )
        self.assertEqual(interruption.reason_code, "WORKER_RESTARTED")
        self.assertEqual(
            result.result_artifact_ids,
            ("artifact-001", "artifact-002"),
        )
        with self.assertRaisesRegex(ValueError, "invalid job transition"):
            replace(
                started,
                from_state=DurableJobState.SUCCEEDED,
                to_state=DurableJobState.RUNNING,
            )

    def test_job_kinds_and_review_required_state_cover_phase_six(self) -> None:
        self.assertEqual(
            {item.value for item in DurableJobKind},
            {
                "source.inspect",
                "odoo.capture",
                "workspace.prepare",
                "workspace.preflight",
                "recipe.apply",
                "report.create",
                "workspace.load",
                "workspace.correct",
                "workspace.reconcile",
                "project.delete",
            },
        )
        review_required = JobStateTransition(
            transition_id="transition-review-001",
            attempt=self.attempt,
            from_state=DurableJobState.RUNNING,
            to_state=DurableJobState.REVIEW_REQUIRED,
            expected_revision=2,
            resulting_revision=3,
            occurred_at=NOW,
            reason_code="DATA_MANAGER_REVIEW_REQUIRED",
        )

        self.assertIs(
            review_required.to_state,
            DurableJobState.REVIEW_REQUIRED,
        )
        with self.assertRaisesRegex(ValueError, "invalid job transition"):
            replace(
                review_required,
                from_state=DurableJobState.REVIEW_REQUIRED,
                to_state=DurableJobState.RUNNING,
            )

    def test_job_transition_requires_one_compare_and_set_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "advance by one"):
            JobStateTransition(
                transition_id="transition-001",
                attempt=self.attempt,
                from_state=DurableJobState.QUEUED,
                to_state=DurableJobState.RUNNING,
                expected_revision=4,
                resulting_revision=6,
                occurred_at=NOW,
            )

    def test_attempt_rejects_reversed_timestamps(self) -> None:
        with self.assertRaisesRegex(ValueError, "heartbeat_at"):
            DurableJobAttempt(
                identity=self.attempt,
                worker_id="worker-001",
                started_at=NOW,
                heartbeat_at=NOW - timedelta(seconds=1),
            )

    def test_attempt_outcome_requires_finish_and_one_disposition(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires finished_at"):
            DurableJobAttempt(
                identity=self.attempt,
                worker_id="worker-001",
                started_at=NOW,
                heartbeat_at=NOW,
                result_receipt_hash=HASH_A,
            )
        with self.assertRaisesRegex(ValueError, "both failure and result"):
            DurableJobAttempt(
                identity=self.attempt,
                worker_id="worker-001",
                started_at=NOW,
                heartbeat_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                failure_code="WORKER_FAILED",
                result_receipt_hash=HASH_A,
            )

    def test_expired_attempt_recovery_is_bound_to_receipt_evidence(self) -> None:
        no_receipt = JobReceiptCheck(
            check_id="receipt-check-none",
            attempt=self.attempt,
            operation_id="operation-001",
            status=JobReceiptCheckStatus.NO_COMMITTED_RECEIPT,
            attempt_expired_at=NOW,
            checked_at=NOW + timedelta(seconds=1),
        )
        matched_receipt = replace(
            no_receipt,
            check_id="receipt-check-matched",
            status=JobReceiptCheckStatus.MATCHED_COMMITTED_RECEIPT,
            data_plane_receipt_hash=HASH_A,
        )
        retryable = JobRecoveryDecision(
            decision_id="decision-retryable",
            receipt_check=no_receipt,
            disposition=JobRecoveryDisposition.INTERRUPT_RETRYABLE,
            decided_at=NOW + timedelta(seconds=2),
            reason_code="NO_COMMITTED_RECEIPT",
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            replace(
                retryable,
                receipt_check=matched_receipt,
            )
        completed = replace(
            retryable,
            decision_id="decision-complete",
            receipt_check=matched_receipt,
            disposition=JobRecoveryDisposition.COMPLETE_FROM_RECEIPT,
        )
        with self.assertRaisesRegex(ValueError, "retryable"):
            JobInterruption(
                interruption_id="interruption-001",
                attempt=self.attempt,
                recovery_decision=completed,
                interrupted_at=NOW + timedelta(seconds=3),
                reason_code="WORKER_RESTARTED",
            )
        with self.assertRaisesRegex(ValueError, "requires a recovery decision"):
            JobStateTransition(
                transition_id="transition-interrupted-without-check",
                attempt=self.attempt,
                from_state=DurableJobState.RUNNING,
                to_state=DurableJobState.INTERRUPTED,
                expected_revision=2,
                resulting_revision=3,
                occurred_at=NOW + timedelta(seconds=3),
            )
        interrupted = JobStateTransition(
            transition_id="transition-interrupted",
            attempt=self.attempt,
            from_state=DurableJobState.RUNNING,
            to_state=DurableJobState.INTERRUPTED,
            expected_revision=2,
            resulting_revision=3,
            occurred_at=NOW + timedelta(seconds=3),
            recovery_decision_id=retryable.decision_id,
        )

        self.assertEqual(interrupted.recovery_decision_id, retryable.decision_id)

    def test_receipt_check_requires_expiry_and_bounded_receipt_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "precede attempt expiry"):
            JobReceiptCheck(
                check_id="receipt-check-early",
                attempt=self.attempt,
                operation_id="operation-001",
                status=JobReceiptCheckStatus.NO_COMMITTED_RECEIPT,
                attempt_expired_at=NOW + timedelta(seconds=1),
                checked_at=NOW,
            )
        with self.assertRaisesRegex(ValueError, "sha256"):
            JobReceiptCheck(
                check_id="receipt-check-invalid-hash",
                attempt=self.attempt,
                operation_id="operation-001",
                status=JobReceiptCheckStatus.RECEIPT_MISMATCH,
                attempt_expired_at=NOW,
                checked_at=NOW + timedelta(seconds=1),
                data_plane_receipt_hash="invalid",
            )


class RetentionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.organization = OrganizationId("organization-001")

    def test_default_is_90_inactive_days_and_customer_can_choose_delete_after_load(
        self,
    ) -> None:
        default = ProjectRetentionSelection.default_for_project(
            organization_id=self.organization,
            project_id="project-001",
            selected_by=identity(),
            selected_at=NOW,
            warning_revision="retention-warning-001",
        )
        after_load = replace(
            default,
            mode=RetentionMode.DELETE_AFTER_VERIFIED_ODOO_LOAD,
            revision=2,
            inactivity_days=None,
        )

        self.assertEqual(default.inactivity_days, DEFAULT_INACTIVITY_RETENTION_DAYS)
        self.assertEqual(
            after_load.mode,
            RetentionMode.DELETE_AFTER_VERIFIED_ODOO_LOAD,
        )

    def test_deletion_result_and_receipt_retain_no_customer_data(self) -> None:
        request = ProjectDeletionRequest(
            deletion_request_id="deletion-001",
            organization_id=self.organization,
            project_id="project-001",
            retention_revision=2,
            confirmation_hash=HASH_A,
            requested_by=identity(),
            requested_at=NOW,
        )
        result = ProjectDeletionResult(
            deletion_request_id=request.deletion_request_id,
            organization_id=self.organization,
            project_id=request.project_id,
            status=DeletionResultStatus.COMPLETED,
            result_code="ALL_CUSTOMER_DATA_REMOVED",
            completed_at=NOW + timedelta(minutes=1),
        )
        receipt = MinimalDeletionReceipt(
            deletion_receipt_id="deletion-receipt-001",
            deletion_request_id=request.deletion_request_id,
            organization_id=self.organization,
            project_id=request.project_id,
            policy_revision="deletion-policy-001",
            completed_by=identity("System deletion worker"),
            completed_at=result.completed_at,
        )

        self.assertEqual(result.status, DeletionResultStatus.COMPLETED)
        self.assertFalse(receipt.customer_data_retained)
        self.assertFalse(hasattr(receipt, "storage_keys"))
        with self.assertRaisesRegex(ValueError, "cannot retain customer data"):
            replace(receipt, customer_data_retained=True)


if __name__ == "__main__":
    unittest.main()
