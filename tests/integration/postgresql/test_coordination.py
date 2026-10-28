"""Verify PostgreSQL operation, receipt, and fenced-lease adapters."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from unittest.mock import Mock, patch

from impodo.adapters.postgresql.coordination import (
    PsycopgOperationJournal,
    PsycopgWorkspaceLeaseRepository,
    open_psycopg_coordination_session,
)
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
ORGANIZATION = OrganizationId("fictional-organization")
OPERATION_ID = "20000000-0000-4000-8000-000000000001"
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "30000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "40000000-0000-4000-8000-000000000001"
LEASE_ID = "50000000-0000-4000-8000-000000000001"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _operation() -> OperationRequest:
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


def _operation_row(request: OperationRequest) -> tuple[object, ...]:
    return (
        request.operation_id,
        str(request.organization_id),
        request.project_id,
        request.operation_kind,
        request.idempotency_key,
        request.semantic_input_hash,
        request.authorization_receipt_id,
        request.requested_by.issuer,
        request.requested_by.subject_id,
        request.requested_by.display_name,
        request.requested_at,
    )


def _snapshot_operation_row(
    request: OperationRequest,
    *,
    state: CrossStoreOperationState,
    receipt_hash: str | None = None,
    interrupted_at: datetime | None = None,
    disposition: RecoveryDisposition | None = None,
) -> tuple[object, ...]:
    return (
        *_operation_row(request),
        state,
        receipt_hash,
        interrupted_at,
        disposition,
    )


def _data_receipt() -> DataPlaneOperationReceipt:
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
                storage_key="organizations/fictional/projects/one/prepared.parquet",
                sha256=HASH_C,
                size_bytes=128,
                media_type="application/vnd.apache.parquet",
            ),
        ),
        committed_at=NOW + timedelta(minutes=1),
    )


def _stored_data_receipt_row(
    receipt: DataPlaneOperationReceipt,
) -> tuple[object, ...]:
    return (
        receipt.receipt_id,
        receipt.operation_id,
        receipt.project_id,
        receipt.workspace_id,
        receipt.operation_kind,
        receipt.semantic_input_hash,
        receipt.fencing_token,
        receipt.evidence_hash,
        receipt.semantic_hash,
        [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_kind": artifact.artifact_kind,
                "media_type": artifact.media_type,
                "owner_id": artifact.owner_id,
                "owner_kind": artifact.owner_kind,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "storage_key": artifact.storage_key,
            }
            for artifact in receipt.artifacts
        ],
        receipt.committed_at,
    )


def _lease_row(
    *,
    token: int = 1,
    heartbeat_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=5),
    version: int = 1,
    released_at: datetime | None = None,
) -> tuple[object, ...]:
    return (
        LEASE_ID,
        str(ORGANIZATION),
        PROJECT_ID,
        WORKSPACE_ID,
        ATTEMPT_ID,
        token,
        NOW,
        heartbeat_at,
        expires_at,
        version,
        released_at,
    )


class _Session:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.events: list[tuple[str, str, tuple[object, ...]]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def fetch_one(self, query, parameters=()):
        self.events.append(("fetch_one", query, parameters))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _journal(session: _Session) -> PsycopgOperationJournal:
    return PsycopgOperationJournal(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


def _leases(session: _Session) -> PsycopgWorkspaceLeaseRepository:
    return PsycopgWorkspaceLeaseRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


class PostgreSQLOperationJournalTests(unittest.TestCase):
    def test_reserve_inserts_once_and_replays_the_original_request(self) -> None:
        request = _operation()
        inserted = _Session([_operation_row(request)])

        result = _journal(inserted).reserve(request)

        self.assertIs(result, request)
        self.assertIn(
            "ON CONFLICT (organization_id, idempotency_key)", inserted.events[0][1]
        )
        self.assertTrue(inserted.committed)
        self.assertTrue(inserted.closed)

        replay = _Session([None, _operation_row(request)])
        replayed = _journal(replay).reserve(
            replace(
                request,
                operation_id="20000000-0000-4000-8000-000000000002",
                requested_at=NOW + timedelta(seconds=1),
            )
        )

        self.assertEqual(replayed, request)
        self.assertTrue(replay.committed)

    def test_reservation_status_distinguishes_new_work_from_replay(self) -> None:
        request = _operation()
        created = _journal(_Session([_operation_row(request)])).reserve_with_status(
            request
        )
        replayed = _journal(
            _Session([None, _operation_row(request)])
        ).reserve_with_status(
            replace(
                request,
                operation_id="20000000-0000-4000-8000-000000000002",
            )
        )

        self.assertTrue(created.created)
        self.assertIs(created.request, request)
        self.assertFalse(replayed.created)
        self.assertEqual(replayed.request, request)

    def test_changed_meaning_under_one_key_rolls_back(self) -> None:
        request = _operation()
        session = _Session([None, _operation_row(request)])

        with self.assertRaisesRegex(
            IdempotencyConflictError,
            "different semantic input",
        ):
            _journal(session).reserve(
                replace(
                    request,
                    operation_id="20000000-0000-4000-8000-000000000002",
                    semantic_input_hash=HASH_B,
                )
            )

        self.assertTrue(session.rolled_back)
        self.assertTrue(session.closed)

    def test_data_receipt_is_bound_once_to_exact_operation_meaning(self) -> None:
        receipt = _data_receipt()
        session = _Session(
            [
                (PROJECT_ID, receipt.operation_kind, HASH_A, "RESERVED", None),
                (receipt.semantic_hash,),
                (OPERATION_ID,),
            ]
        )

        _journal(session).record_data_plane_receipt(receipt)

        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("FOR UPDATE", queries)
        self.assertIn("INSERT INTO impodo_control.data_plane_receipt", queries)
        self.assertIn("state = 'DATA_COMMITTED'", queries)
        self.assertNotIn("DELETE FROM", queries)
        self.assertTrue(session.committed)

        replay = _Session(
            [
                (
                    PROJECT_ID,
                    receipt.operation_kind,
                    HASH_A,
                    "DATA_COMMITTED",
                    receipt.semantic_hash,
                )
            ]
        )
        _journal(replay).record_data_plane_receipt(receipt)
        self.assertEqual(len(replay.events), 1)
        self.assertTrue(replay.committed)

    def test_substituted_data_receipt_rolls_back_without_row_detail(self) -> None:
        receipt = _data_receipt()
        session = _Session(
            [
                (
                    PROJECT_ID,
                    receipt.operation_kind,
                    HASH_A,
                    "DATA_COMMITTED",
                    HASH_C,
                )
            ]
        )

        with self.assertRaisesRegex(
            ReceiptMismatchError,
            "different data-plane evidence",
        ):
            _journal(session).record_data_plane_receipt(receipt)

        self.assertTrue(session.rolled_back)

    def test_projection_receipt_requires_exact_stored_data_receipt(self) -> None:
        data_receipt = _data_receipt()
        projection = ControlPlaneProjectionReceipt.for_data_plane(
            data_receipt,
            projection_receipt_id="projection-001",
            projection_hash=HASH_C,
            projection_revision=2,
            projected_at=NOW + timedelta(minutes=2),
        )
        session = _Session(
            [
                (
                    PROJECT_ID,
                    data_receipt.operation_kind,
                    HASH_A,
                    "DATA_COMMITTED",
                    data_receipt.semantic_hash,
                ),
                None,
                (projection.projection_receipt_id,),
                (OPERATION_ID,),
            ]
        )

        _journal(session).record_projection_receipt(projection)

        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("INSERT INTO impodo_control.projection_receipt", queries)
        self.assertIn("state = 'PROJECTED'", queries)
        self.assertTrue(session.committed)

        substituted = replace(projection, data_plane_receipt_hash=HASH_A)
        mismatch = _Session(
            [
                (
                    PROJECT_ID,
                    data_receipt.operation_kind,
                    HASH_A,
                    "DATA_COMMITTED",
                    data_receipt.semantic_hash,
                )
            ]
        )
        with self.assertRaises(ReceiptMismatchError):
            _journal(mismatch).record_projection_receipt(substituted)
        self.assertTrue(mismatch.rolled_back)

    def test_get_reconstructs_exact_projected_receipt_evidence(self) -> None:
        request = _operation()
        data_receipt = _data_receipt()
        projection = ControlPlaneProjectionReceipt.for_data_plane(
            data_receipt,
            projection_receipt_id="projection-001",
            projection_hash=HASH_C,
            projection_revision=2,
            projected_at=NOW + timedelta(minutes=2),
        )
        session = _Session(
            [
                _snapshot_operation_row(
                    request,
                    state=CrossStoreOperationState.PROJECTED,
                    receipt_hash=data_receipt.semantic_hash,
                ),
                _stored_data_receipt_row(data_receipt),
                (
                    projection.projection_receipt_id,
                    projection.operation_id,
                    projection.data_plane_receipt_hash,
                    projection.projection_hash,
                    projection.projection_revision,
                    projection.projected_at,
                ),
            ]
        )

        snapshot = _journal(session).get(request)

        self.assertEqual(snapshot.state, CrossStoreOperationState.PROJECTED)
        self.assertEqual(snapshot.data_plane_receipt, data_receipt)
        self.assertEqual(snapshot.projection_receipt, projection)
        self.assertEqual(len(session.events), 3)
        self.assertTrue(session.committed)

    def test_recovery_states_are_bounded_idempotent_and_never_downgrade(self) -> None:
        request = _operation()
        interrupted_at = NOW + timedelta(minutes=3)
        interrupted = _Session(
            [
                (
                    PROJECT_ID,
                    request.operation_kind,
                    HASH_A,
                    "RESERVED",
                    None,
                    None,
                    None,
                ),
                (OPERATION_ID,),
            ]
        )

        _journal(interrupted).record_interruption(
            request,
            interrupted_at=interrupted_at,
            disposition=RecoveryDisposition.NO_COMMITTED_RECEIPT,
        )

        query = interrupted.events[1][1]
        parameters = interrupted.events[1][2]
        self.assertIn("data_plane_receipt_hash IS NOT DISTINCT FROM", query)
        self.assertEqual(parameters[0], CrossStoreOperationState.INTERRUPTED)
        self.assertEqual(parameters[2], RecoveryDisposition.NO_COMMITTED_RECEIPT)
        self.assertTrue(interrupted.committed)

        replay = _Session(
            [
                (
                    PROJECT_ID,
                    request.operation_kind,
                    HASH_A,
                    "INTERRUPTED",
                    None,
                    interrupted_at,
                    RecoveryDisposition.NO_COMMITTED_RECEIPT,
                )
            ]
        )
        _journal(replay).record_interruption(
            request,
            interrupted_at=interrupted_at,
            disposition=RecoveryDisposition.NO_COMMITTED_RECEIPT,
        )
        self.assertEqual(len(replay.events), 1)

        committed = _Session(
            [
                (
                    PROJECT_ID,
                    request.operation_kind,
                    HASH_A,
                    "DATA_COMMITTED",
                    HASH_B,
                    None,
                    None,
                )
            ]
        )
        with self.assertRaisesRegex(
            OperationStateConflictError,
            "Committed data evidence",
        ):
            _journal(committed).record_interruption(
                request,
                interrupted_at=interrupted_at,
                disposition=RecoveryDisposition.NO_COMMITTED_RECEIPT,
            )
        self.assertTrue(committed.rolled_back)


class PostgreSQLWorkspaceLeaseTests(unittest.TestCase):
    def test_acquire_heartbeat_and_release_use_exact_fencing_predicates(self) -> None:
        acquired_session = _Session([_lease_row()])
        lease = _leases(acquired_session).acquire(
            organization_id=ORGANIZATION,
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            holder_attempt_id=ATTEMPT_ID,
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self.assertEqual(lease.fencing_token, 1)
        self.assertIn("fencing_token + 1", acquired_session.events[0][1])

        heartbeat_at = NOW + timedelta(minutes=1)
        heartbeat_expiry = NOW + timedelta(minutes=8)
        heartbeat_session = _Session(
            [
                _lease_row(
                    heartbeat_at=heartbeat_at,
                    expires_at=heartbeat_expiry,
                    version=2,
                )
            ]
        )
        heartbeat = _leases(heartbeat_session).heartbeat(
            lease,
            at=heartbeat_at,
            expires_at=heartbeat_expiry,
        )
        heartbeat_query = heartbeat_session.events[0][1]
        self.assertIn("fencing_token = %s", heartbeat_query)
        self.assertIn("version = %s", heartbeat_query)
        self.assertEqual(heartbeat.version, 2)

        released_at = NOW + timedelta(minutes=2)
        release_session = _Session(
            [
                _lease_row(
                    heartbeat_at=heartbeat_at,
                    expires_at=heartbeat_expiry,
                    version=3,
                    released_at=released_at,
                )
            ]
        )
        released = _leases(release_session).release(heartbeat, at=released_at)
        self.assertEqual(released.version, 3)
        self.assertEqual(released.released_at, released_at)

    def test_active_or_stale_lease_returns_one_bounded_conflict(self) -> None:
        active = _Session([None])
        with self.assertRaisesRegex(StaleLeaseError, "active lease"):
            _leases(active).acquire(
                organization_id=ORGANIZATION,
                project_id=PROJECT_ID,
                workspace_id=WORKSPACE_ID,
                holder_attempt_id=ATTEMPT_ID,
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        self.assertTrue(active.rolled_back)

        lease = WorkspaceLease(
            lease_id=LEASE_ID,
            organization_id=ORGANIZATION,
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            holder_attempt_id=ATTEMPT_ID,
            fencing_token=1,
            acquired_at=NOW,
            heartbeat_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        stale = _Session([None])
        with self.assertRaisesRegex(StaleLeaseError, "fenced"):
            _leases(stale).heartbeat(
                lease,
                at=NOW + timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=8),
            )
        self.assertTrue(stale.rolled_back)

    def test_default_session_is_bounded_and_uses_runtime_role(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.coordination.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_coordination_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertEqual(kwargs["application_name"], "impodo-coordination")
        self.assertIn("role=impodo_runtime", kwargs["options"])
        self.assertIn("statement_timeout=5000", kwargs["options"])
        self.assertIn("lock_timeout=1000", kwargs["options"])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
