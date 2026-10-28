"""Exercise durable coordination against disposable PostgreSQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import PurePosixPath
import unittest

import psycopg

from impodo.adapters.postgresql.coordination import (
    PsycopgOperationJournal,
    PsycopgWorkspaceLeaseRepository,
)
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.coordination import (
    ArtifactIdentity,
    ControlPlaneProjectionReceipt,
    CrossStoreOperationState,
    DataPlaneOperationReceipt,
    IdempotencyConflictError,
    OperationRequest,
    OperationStateConflictError,
    ReceiptMismatchError,
    RecoveryDisposition,
    StaleLeaseError,
    WorkspaceLease,
)
from impodo.domain.shared.tenancy import OrganizationId


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION_A = OrganizationId("fictional-organization-a")
ORGANIZATION_B = OrganizationId("fictional-organization-b")
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "30000000-0000-4000-8000-000000000001"
OPERATION_ID = "20000000-0000-4000-8000-000000000001"
ATTEMPT_A = "40000000-0000-4000-8000-000000000001"
ATTEMPT_B = "40000000-0000-4000-8000-000000000002"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _operation(
    organization_id: OrganizationId = ORGANIZATION_A,
    *,
    operation_id: str = OPERATION_ID,
    semantic_input_hash: str = HASH_A,
) -> OperationRequest:
    return OperationRequest(
        operation_id=operation_id,
        organization_id=organization_id,
        project_id=PROJECT_ID,
        operation_kind="workspace.prepare",
        idempotency_key="prepare:workspace:revision-1",
        semantic_input_hash=semantic_input_hash,
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
        organization_id=ORGANIZATION_A,
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
                storage_key="organizations/a/projects/one/prepared.parquet",
                sha256=HASH_C,
                size_bytes=128,
                media_type="application/vnd.apache.parquet",
            ),
        ),
        committed_at=NOW + timedelta(minutes=1),
    )


class PostgreSQLCoordinationLiveTests(unittest.TestCase):
    """Use only fictional rows in the Linux-owned disposable database."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("IMPODO_TEST_POSTGRES_DSN", "")
        if not cls.dsn:
            raise RuntimeError(
                "IMPODO_TEST_POSTGRES_DSN must name a disposable PostgreSQL database"
            )
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_roles WHERE rolname = 'impodo_runtime'
                    ) THEN
                        CREATE ROLE impodo_runtime NOLOGIN NOSUPERUSER NOCREATEDB
                            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                    END IF;
                END
                $$
                """
            )
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")
            connection.execute("CREATE SCHEMA impodo_control")
            for migration in POSTGRESQL_MIGRATIONS:
                for statement in migration.statements:
                    connection.execute(statement.query, statement.parameters)
        reference = PurePosixPath("/run/secrets/postgres-runtime-dsn")
        cls.journal = PsycopgOperationJournal(
            lambda _reference: cls.dsn,
            reference,
        )
        cls.leases = PsycopgWorkspaceLeaseRepository(
            lambda _reference: cls.dsn,
            reference,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")

    def setUp(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            for table in (
                "projection_receipt",
                "data_plane_receipt",
                "workspace_lease",
                "operation_request",
                "project_catalogue",
            ):
                connection.execute(f"TRUNCATE impodo_control.{table} CASCADE")
            connection.execute("DELETE FROM impodo_control.organization")
            for organization_id, display_name in (
                (ORGANIZATION_A, "Fictional organization A"),
                (ORGANIZATION_B, "Fictional organization B"),
            ):
                connection.execute(
                    """
                    INSERT INTO impodo_control.organization (
                        organization_id, display_name, status,
                        policy_revision, optimistic_revision, deletion_state
                    ) VALUES (%s, %s, 'ACTIVE', 1, 1, 'NONE')
                    """,
                    (str(organization_id), display_name),
                )
                connection.execute(
                    """
                    INSERT INTO impodo_control.project_catalogue (
                        organization_id, project_id, display_name, status,
                        data_classification, retention_days,
                        optimistic_revision, catalogue_state
                    ) VALUES (%s, %s, %s, 'DRAFT', 'CONFIDENTIAL', 365, 1, 'PENDING')
                    """,
                    (str(organization_id), PROJECT_ID, display_name),
                )

    def test_idempotency_is_scoped_by_organization_and_semantic_meaning(self) -> None:
        first = self.journal.reserve(_operation())
        replayed = self.journal.reserve(
            _operation(operation_id="20000000-0000-4000-8000-000000000002")
        )
        other_organization = self.journal.reserve(_operation(ORGANIZATION_B))

        self.assertEqual(replayed, first)
        self.assertEqual(other_organization.operation_id, OPERATION_ID)
        self.assertEqual(other_organization.organization_id, ORGANIZATION_B)
        with self.assertRaises(IdempotencyConflictError):
            self.journal.reserve(
                _operation(
                    operation_id="20000000-0000-4000-8000-000000000003",
                    semantic_input_hash=HASH_B,
                )
            )
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                """
                SELECT organization_id, count(*)
                FROM impodo_control.operation_request
                GROUP BY organization_id
                ORDER BY organization_id
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                (str(ORGANIZATION_A), 1),
                (str(ORGANIZATION_B), 1),
            ],
        )

    def test_receipts_are_immutable_replayable_and_exactly_bound(self) -> None:
        self.journal.reserve(_operation())
        data_receipt = _data_receipt()
        self.journal.record_data_plane_receipt(data_receipt)
        self.journal.record_data_plane_receipt(data_receipt)
        projection = ControlPlaneProjectionReceipt.for_data_plane(
            data_receipt,
            projection_receipt_id="projection-001",
            projection_hash=HASH_C,
            projection_revision=2,
            projected_at=NOW + timedelta(minutes=2),
        )
        self.journal.record_projection_receipt(projection)
        self.journal.record_projection_receipt(projection)

        snapshot = self.journal.get(_operation())

        with self.assertRaises(ReceiptMismatchError):
            self.journal.record_projection_receipt(
                replace(projection, data_plane_receipt_hash=HASH_A)
            )
        with psycopg.connect(self.dsn) as connection:
            state, data_count, projection_count, runtime_can_delete = (
                connection.execute(
                    """
                    SELECT o.state,
                           (SELECT count(*) FROM impodo_control.data_plane_receipt),
                           (SELECT count(*) FROM impodo_control.projection_receipt),
                           has_table_privilege(
                               'impodo_runtime',
                               'impodo_control.data_plane_receipt',
                               'DELETE'
                           )
                    FROM impodo_control.operation_request o
                    WHERE o.organization_id = %s AND o.operation_id = %s
                    """,
                    (str(ORGANIZATION_A), OPERATION_ID),
                ).fetchone()
            )
        self.assertEqual(state, "PROJECTED")
        self.assertEqual(snapshot.state, CrossStoreOperationState.PROJECTED)
        self.assertEqual(snapshot.data_plane_receipt, data_receipt)
        self.assertEqual(snapshot.projection_receipt, projection)
        self.assertEqual(data_count, 1)
        self.assertEqual(projection_count, 1)
        self.assertFalse(runtime_can_delete)

    def test_interruption_and_manual_review_are_durable_terminal_states(self) -> None:
        interrupted = replace(
            _operation(),
            operation_id="20000000-0000-4000-8000-000000000010",
            idempotency_key="prepare:workspace:interrupted",
        )
        interrupted_at = NOW + timedelta(minutes=3)
        self.journal.reserve(interrupted)
        self.journal.record_interruption(
            interrupted,
            interrupted_at=interrupted_at,
            disposition=RecoveryDisposition.NO_COMMITTED_RECEIPT,
        )
        interruption = self.journal.get(interrupted)

        self.assertEqual(
            interruption.state,
            CrossStoreOperationState.INTERRUPTED,
        )
        self.assertEqual(interruption.interrupted_at, interrupted_at)
        self.assertEqual(
            interruption.recovery_disposition,
            RecoveryDisposition.NO_COMMITTED_RECEIPT,
        )

        review_request = replace(
            _operation(),
            operation_id="20000000-0000-4000-8000-000000000011",
            idempotency_key="prepare:workspace:manual-review",
        )
        review_receipt = replace(
            _data_receipt(),
            operation_id=review_request.operation_id,
        )
        self.journal.reserve(review_request)
        self.journal.record_data_plane_receipt(review_receipt)
        with self.assertRaises(OperationStateConflictError):
            self.journal.record_interruption(
                review_request,
                interrupted_at=interrupted_at,
                disposition=RecoveryDisposition.NO_COMMITTED_RECEIPT,
            )
        self.journal.require_manual_review(
            review_request,
            interrupted_at=interrupted_at,
            disposition=RecoveryDisposition.ARTIFACT_MISMATCH,
        )
        review = self.journal.get(review_request)

        self.assertEqual(review.state, CrossStoreOperationState.MANUAL_REVIEW)
        self.assertEqual(review.data_plane_receipt, review_receipt)
        self.assertEqual(
            review.recovery_disposition,
            RecoveryDisposition.ARTIFACT_MISMATCH,
        )

    def test_lease_reacquisition_increments_token_and_fences_old_owner(self) -> None:
        first = self.leases.acquire(
            organization_id=ORGANIZATION_A,
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            holder_attempt_id=ATTEMPT_A,
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        with self.assertRaises(StaleLeaseError):
            self.leases.acquire(
                organization_id=ORGANIZATION_A,
                project_id=PROJECT_ID,
                workspace_id=WORKSPACE_ID,
                holder_attempt_id=ATTEMPT_B,
                acquired_at=NOW + timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=6),
            )
        heartbeat = self.leases.heartbeat(
            first,
            at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=8),
        )
        released = self.leases.release(
            heartbeat,
            at=NOW + timedelta(minutes=2),
        )
        second = self.leases.acquire(
            organization_id=ORGANIZATION_A,
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            holder_attempt_id=ATTEMPT_B,
            acquired_at=NOW + timedelta(minutes=3),
            expires_at=NOW + timedelta(minutes=9),
        )
        other_organization = self.leases.acquire(
            organization_id=ORGANIZATION_B,
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            holder_attempt_id=ATTEMPT_A,
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

        self.assertEqual(released.fencing_token, 1)
        self.assertEqual(second.fencing_token, 2)
        self.assertEqual(other_organization.fencing_token, 1)
        with self.assertRaises(StaleLeaseError):
            self.leases.heartbeat(
                first,
                at=NOW + timedelta(minutes=4),
                expires_at=NOW + timedelta(minutes=10),
            )

    def test_two_concurrent_contenders_produce_one_active_lease(self) -> None:
        def acquire(attempt_id: str) -> WorkspaceLease | None:
            try:
                return self.leases.acquire(
                    organization_id=ORGANIZATION_A,
                    project_id=PROJECT_ID,
                    workspace_id=WORKSPACE_ID,
                    holder_attempt_id=attempt_id,
                    acquired_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                )
            except StaleLeaseError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(acquire, (ATTEMPT_A, ATTEMPT_B)))

        winners = tuple(result for result in results if result is not None)
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].fencing_token, 1)
        with psycopg.connect(self.dsn) as connection:
            active_count = connection.execute(
                """
                SELECT count(*) FROM impodo_control.workspace_lease
                WHERE organization_id = %s AND project_id = %s
                  AND workspace_id = %s AND released_at IS NULL
                """,
                (str(ORGANIZATION_A), PROJECT_ID, WORKSPACE_ID),
            ).fetchone()[0]
        self.assertEqual(active_count, 1)


if __name__ == "__main__":
    unittest.main()
