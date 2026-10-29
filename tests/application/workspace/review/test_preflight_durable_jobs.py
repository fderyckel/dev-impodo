"""Verify the portable Final review durable-job binding."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import MagicMock

from impodo.application.preflight_attempts import PreflightComparisonRequest
from impodo.application.preflight_durable_jobs import (
    DurablePreflightJobConflictError,
    DurablePreflightJobService,
    DurablePreflightSubmission,
)
from impodo.application.shared.jobs import DurableJobControl
from impodo.domain.shared.access import (
    ActorIdentity,
    AuthorizationError,
    Capability,
)
from impodo.domain.shared.jobs import DurableJobKind
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    ProjectVisibilityScope,
)

NOW = datetime(2026, 10, 29, 9, 30, tzinfo=UTC)
ORGANIZATION_ID = OrganizationId("fictional-organization")
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
DATA_VERSION_ID = "30000000-0000-4000-8000-000000000001"
MIGRATION_RUN_ID = "40000000-0000-4000-8000-000000000001"
OPERATION_ID = "50000000-0000-4000-8000-000000000001"
JOB_ID = "60000000-0000-4000-8000-000000000001"
SEMANTIC_HASH = "sha256:" + "a" * 64
COMMAND_MANIFEST_HASH = "sha256:" + "b" * 64


def _comparison() -> PreflightComparisonRequest:
    return PreflightComparisonRequest(
        operation_id=OPERATION_ID,
        project_id=PROJECT_ID,
        workspace_id=WORKSPACE_ID,
        semantic_input_hash=SEMANTIC_HASH,
        workspace_revision=7,
        data_version_id=DATA_VERSION_ID,
        migration_run_id=MIGRATION_RUN_ID,
        recipe_application_id=None,
        requested_at=NOW,
    )


def _authorization() -> AuthorizationReceipt:
    actor_identity = ActorIdentity(
        issuer="urn:impodo:test",
        subject_id="fictional-data-manager",
        display_name="Fictional data manager",
    )
    return AuthorizationReceipt(
        receipt_id="authorization-001",
        principal_id="fictional-data-manager",
        actor_identity=actor_identity,
        organization_id=ORGANIZATION_ID,
        capabilities=frozenset({Capability.PREFLIGHT_RUN}),
        project_visibility=ProjectVisibilityScope(
            organization_id=ORGANIZATION_ID,
            project_ids=frozenset({PROJECT_ID}),
        ),
        role_revision_ids=("role-revision-003",),
        grant_revision_ids=("grant-revision-005",),
        policy_revision="policy-revision-009",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=15),
    )


def _submission() -> DurablePreflightSubmission:
    return DurablePreflightSubmission(
        comparison=_comparison(),
        job_id=JOB_ID,
        idempotency_key=f"preflight:{OPERATION_ID}",
        command_manifest_hash=COMMAND_MANIFEST_HASH,
        authorization=_authorization(),
        authorization_revision=9,
    )


class DurablePreflightJobTests(unittest.TestCase):
    def test_maps_frozen_comparison_and_authorization_exactly(self) -> None:
        request = _submission().to_job_request()

        self.assertEqual(request.job_id, JOB_ID)
        self.assertEqual(request.operation_id, OPERATION_ID)
        self.assertEqual(request.organization_id, ORGANIZATION_ID)
        self.assertEqual(request.project_id, PROJECT_ID)
        self.assertEqual(request.workspace_id, WORKSPACE_ID)
        self.assertIs(request.job_kind, DurableJobKind.PREFLIGHT)
        self.assertEqual(request.idempotency_key, f"preflight:{OPERATION_ID}")
        self.assertEqual(request.semantic_input_hash, SEMANTIC_HASH)
        self.assertEqual(request.manifest_hash, COMMAND_MANIFEST_HASH)
        self.assertEqual(request.authorization_receipt_id, "authorization-001")
        self.assertEqual(request.authorization_revision, 9)
        self.assertEqual(request.requested_by, _authorization().actor_identity)
        self.assertEqual(request.requested_at, NOW)

    def test_enqueue_accepts_only_an_exact_idempotent_replay(self) -> None:
        jobs = MagicMock(spec=DurableJobControl)
        submission = _submission()
        expected = submission.to_job_request()
        jobs.enqueue.return_value = expected

        accepted = DurablePreflightJobService(jobs).enqueue(submission)

        self.assertEqual(accepted, expected)
        jobs.enqueue.assert_called_once_with(expected)

    def test_enqueue_rejects_substituted_control_plane_request(self) -> None:
        jobs = MagicMock(spec=DurableJobControl)
        submission = _submission()
        jobs.enqueue.return_value = replace(
            submission.to_job_request(),
            semantic_input_hash="sha256:" + "c" * 64,
        )

        with self.assertRaisesRegex(
            DurablePreflightJobConflictError,
            "different request facts",
        ):
            DurablePreflightJobService(jobs).enqueue(submission)

    def test_denies_missing_capability_before_job_control(self) -> None:
        jobs = MagicMock(spec=DurableJobControl)
        submission = replace(
            _submission(),
            authorization=replace(
                _authorization(),
                capabilities=frozenset({Capability.PROJECT_VIEW}),
            ),
        )

        with self.assertRaisesRegex(AuthorizationError, "not authorized"):
            DurablePreflightJobService(jobs).enqueue(submission)

        jobs.enqueue.assert_not_called()

    def test_denies_request_that_predates_authorization(self) -> None:
        jobs = MagicMock(spec=DurableJobControl)
        submission = replace(
            _submission(),
            authorization=replace(
                _authorization(),
                issued_at=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(minutes=15),
            ),
        )

        with self.assertRaisesRegex(AuthorizationError, "issued after"):
            DurablePreflightJobService(jobs).enqueue(submission)

        jobs.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
