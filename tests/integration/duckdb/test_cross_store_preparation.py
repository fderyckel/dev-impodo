"""Exercise the concrete Preparation data plane with repository demo data."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.verification import LocalArtifactEvidenceVerifier
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.preparation_operation import (
    DuckDbPreparationDataPlaneCommand,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.scenarios.loader import load_scenario
from impodo.application.shared.cross_store_coordinator import (
    ArtifactEvidenceMismatchError,
    CrossStoreBoundary,
)
from impodo.application.shared.receipt_projection import ReceiptFactsProjectionUpdater
from impodo.domain.shared.access import LOCAL_ACTOR, ActorIdentity
from impodo.domain.shared.coordination import (
    OperationRequest,
    ReceiptMismatchError,
    StaleLeaseError,
    WorkspaceLease,
)
from impodo.domain.shared.tenancy import OrganizationId
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
    WorkspaceStatus,
)
from tests.support.paths import REPOSITORY_ROOT

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION = OrganizationId("fictional-organization")
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "40000000-0000-4000-8000-000000000001"
SCENARIO_PATH = (
    REPOSITORY_ROOT / "scenarios/contact-round-trip/v1/scenario.yaml"
)


class _Clock:
    def __init__(self) -> None:
        self.current = NOW + timedelta(seconds=1)

    def __call__(self) -> datetime:
        current = self.current
        self.current += timedelta(seconds=1)
        return current


class _Fault:
    def __init__(self, boundary: CrossStoreBoundary | None = None) -> None:
        self.boundary = boundary
        self.reached: list[CrossStoreBoundary] = []

    def __call__(self, boundary: CrossStoreBoundary) -> None:
        self.reached.append(boundary)
        if boundary is self.boundary:
            raise RuntimeError(f"injected failure at {boundary}")


class DuckDbCrossStorePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.artifacts = LocalArtifactStore(self.temporary.name)
        self.workspace_id = str(uuid4())
        WorkspaceStateRepository(self.database).initialize_workbench(
            WorkspaceState(
                workspace_id=self.workspace_id,
                name="Fictional shared-server trial",
                source_system="CSV",
                odoo_connection_mode=OdooConnectionMode.LOCAL,
                odoo_base_url="http://127.0.0.1:8069",
                odoo_database="fictional_odoo",
                intended_models=("res.partner",),
                status=WorkspaceStatus.REGISTERED,
                registered_at=NOW,
            ),
            actor=LOCAL_ACTOR,
        )
        self.scenario = load_scenario(SCENARIO_PATH)
        self.clock = _Clock()
        self.command = DuckDbPreparationDataPlaneCommand(
            self.database,
            self.artifacts,
            self.scenario,
            self.workspace_id,
            clock=self.clock,
        )
        self.request = OperationRequest(
            operation_id="20000000-0000-4000-8000-000000000001",
            organization_id=ORGANIZATION,
            project_id=PROJECT_ID,
            operation_kind="workspace.prepare",
            idempotency_key="prepare:contact-demo:revision-1",
            semantic_input_hash=self.scenario.scenario_hash,
            authorization_receipt_id="authorization-001",
            requested_by=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="fictional-user",
                display_name="Fictional user",
            ),
            requested_at=NOW,
        )
        self.lease = WorkspaceLease(
            lease_id="50000000-0000-4000-8000-000000000001",
            organization_id=ORGANIZATION,
            project_id=PROJECT_ID,
            workspace_id=self.workspace_id,
            holder_attempt_id=ATTEMPT_ID,
            fencing_token=1,
            acquired_at=NOW,
            heartbeat_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_one_verified_parquet_receipt_with_one_duckdb_connection(
        self,
    ) -> None:
        fault = _Fault()
        with patch.object(
            self.database.connection_factory,
            "connect",
            wraps=self.database.connection_factory.connect,
        ) as connect:
            receipt = self.command.execute(self.request, self.lease, fault)

        self.assertEqual(connect.call_count, 1)
        self.assertEqual(
            fault.reached,
            [
                CrossStoreBoundary.AFTER_ARTIFACT_STAGING,
                CrossStoreBoundary.AFTER_DATA_PLANE_COMMIT,
                CrossStoreBoundary.AFTER_ARTIFACT_PUBLICATION,
            ],
        )
        self.assertEqual(self.command.find_receipt(self.request), receipt)
        self.assertEqual(receipt.fencing_token, 1)
        self.assertEqual(len(receipt.artifacts), 1)
        LocalArtifactEvidenceVerifier(self.artifacts).verify(receipt.artifacts[0])
        with self.artifacts.materialize_prepared_snapshot(
            self.workspace_id,
            receipt.artifacts[0].storage_key,
            expected_sha256=receipt.artifacts[0].sha256,
        ) as path, duckdb.connect(":memory:") as connection:
            row_count = connection.execute(
                "SELECT count(*) FROM read_parquet(?)",
                [str(path)],
            ).fetchone()
        self.assertEqual(row_count, (1,))

        projection = ReceiptFactsProjectionUpdater().project(receipt)
        self.assertEqual(
            ReceiptFactsProjectionUpdater().project(receipt),
            projection,
        )
        projection.require_data_plane_receipt(receipt)

    def test_failure_after_staging_leaves_no_receipt_or_artifact(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "AFTER_ARTIFACT_STAGING"):
            self.command.execute(
                self.request,
                self.lease,
                _Fault(CrossStoreBoundary.AFTER_ARTIFACT_STAGING),
            )

        self.assertIsNone(self.command.find_receipt(self.request))

    def test_failure_after_commit_retains_receipt_but_not_unpublished_bytes(
        self,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "AFTER_DATA_PLANE_COMMIT"):
            self.command.execute(
                self.request,
                self.lease,
                _Fault(CrossStoreBoundary.AFTER_DATA_PLANE_COMMIT),
            )

        receipt = self.command.find_receipt(self.request)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        with self.assertRaises(ArtifactEvidenceMismatchError):
            LocalArtifactEvidenceVerifier(self.artifacts).verify(
                receipt.artifacts[0]
            )

    def test_failure_after_publication_recovers_exact_receipt_and_bytes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "AFTER_ARTIFACT_PUBLICATION"):
            self.command.execute(
                self.request,
                self.lease,
                _Fault(CrossStoreBoundary.AFTER_ARTIFACT_PUBLICATION),
            )

        receipt = self.command.find_receipt(self.request)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        LocalArtifactEvidenceVerifier(self.artifacts).verify(receipt.artifacts[0])

    def test_request_and_fencing_mismatches_fail_closed(self) -> None:
        with self.assertRaises(ReceiptMismatchError):
            self.command.execute(
                replace(
                    self.request,
                    semantic_input_hash="sha256:" + "0" * 64,
                ),
                self.lease,
                _Fault(),
            )

        self.command.execute(self.request, self.lease, _Fault())
        stale_request = replace(
            self.request,
            operation_id="20000000-0000-4000-8000-000000000002",
            idempotency_key="prepare:contact-demo:revision-2",
        )
        with self.assertRaises(StaleLeaseError):
            self.command.execute(stale_request, self.lease, _Fault())


if __name__ == "__main__":
    unittest.main()
