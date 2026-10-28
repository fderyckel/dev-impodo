"""Run all seven concrete Preparation boundaries against PostgreSQL."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import uuid4

import duckdb
import psycopg

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.verification import LocalArtifactEvidenceVerifier
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.preparation_operation import (
    DuckDbPreparationDataPlaneCommand,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.postgresql.coordination import (
    PsycopgOperationJournal,
    PsycopgWorkspaceLeaseRepository,
)
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.adapters.scenarios.loader import load_scenario
from impodo.application.shared.cross_store_coordinator import (
    CrossStoreBoundary,
    CrossStoreManualReviewError,
    CrossStoreOperationCoordinator,
    CrossStoreOperationInterruptedError,
    WorkspaceCrossStoreCommand,
)
from impodo.application.shared.receipt_projection import ReceiptFactsProjectionUpdater
from impodo.domain.shared.access import LOCAL_ACTOR, ActorIdentity
from impodo.domain.shared.coordination import (
    CrossStoreOperationState,
    OperationRequest,
    RecoveryDisposition,
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
SCENARIO_PATH = (
    REPOSITORY_ROOT / "scenarios/contact-round-trip/v1/scenario.yaml"
)
PLW_SCENARIO_PATH = (
    REPOSITORY_ROOT
    / "scenarios/plw-manufacturing-preparation/v1/scenario.yaml"
)


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        current = self.current
        self.current += timedelta(seconds=1)
        return current


class _OneShotFault:
    def __init__(self, boundary: CrossStoreBoundary) -> None:
        self.boundary = boundary
        self.raised = False

    def __call__(self, boundary: CrossStoreBoundary) -> None:
        if boundary is self.boundary and not self.raised:
            self.raised = True
            raise RuntimeError(f"injected failure at {boundary}")


class PostgreSQLPreparationCrossStoreLiveTests(unittest.TestCase):
    """Use fictional rows and the repository contact demo in disposable stores."""

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

        def read_dsn(reference: PurePosixPath) -> str:
            del reference
            return cls.dsn

        cls.journal = PsycopgOperationJournal(
            read_dsn,
            reference,
        )
        cls.leases = PsycopgWorkspaceLeaseRepository(
            read_dsn,
            reference,
        )
        cls.scenario = load_scenario(SCENARIO_PATH)
        cls.plw_scenario = load_scenario(PLW_SCENARIO_PATH)

    @classmethod
    def tearDownClass(cls) -> None:
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")

    def test_every_concrete_boundary_has_deterministic_recovery(self) -> None:
        interrupted_boundaries = {
            CrossStoreBoundary.BEFORE_LEASE,
            CrossStoreBoundary.AFTER_LEASE,
            CrossStoreBoundary.AFTER_ARTIFACT_STAGING,
        }
        successful_boundaries = {
            CrossStoreBoundary.AFTER_ARTIFACT_PUBLICATION,
            CrossStoreBoundary.BEFORE_PROJECTION_UPDATE,
            CrossStoreBoundary.AFTER_PROJECTION_UPDATE,
        }
        for boundary in CrossStoreBoundary:
            with self.subTest(boundary=boundary):
                self._reset_control_plane()
                with tempfile.TemporaryDirectory() as root:
                    database = DuckDbWorkspaceDatabase(root)
                    artifacts = LocalArtifactStore(root)
                    workspace_id = str(uuid4())
                    WorkspaceStateRepository(database).initialize_workbench(
                        WorkspaceState(
                            workspace_id=workspace_id,
                            name="Fictional PostgreSQL trial",
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
                    request = OperationRequest(
                        operation_id=str(uuid4()),
                        organization_id=ORGANIZATION,
                        project_id=PROJECT_ID,
                        operation_kind="workspace.prepare",
                        idempotency_key=f"prepare:{boundary.value.casefold()}",
                        semantic_input_hash=self.scenario.scenario_hash,
                        authorization_receipt_id="authorization-001",
                        requested_by=ActorIdentity(
                            issuer="urn:impodo:test",
                            subject_id="fictional-user",
                            display_name="Fictional user",
                        ),
                        requested_at=NOW,
                    )
                    clock = _Clock()
                    fault = _OneShotFault(boundary)
                    coordinator = CrossStoreOperationCoordinator(
                        journal=self.journal,
                        leases=self.leases,
                        data_plane=DuckDbPreparationDataPlaneCommand(
                            database,
                            artifacts,
                            self.scenario,
                            workspace_id,
                            clock=clock,
                        ),
                        artifacts=LocalArtifactEvidenceVerifier(artifacts),
                        projections=ReceiptFactsProjectionUpdater(),
                        checkpoint=fault,
                        clock=clock,
                    )
                    command = WorkspaceCrossStoreCommand(
                        request=request,
                        workspace_id=workspace_id,
                        holder_attempt_id=str(uuid4()),
                    )

                    with self.assertRaisesRegex(RuntimeError, boundary.value):
                        coordinator.execute(command)

                    if boundary in interrupted_boundaries:
                        with self.assertRaises(CrossStoreOperationInterruptedError):
                            coordinator.execute(command)
                        snapshot = self.journal.get(request)
                        self.assertEqual(
                            snapshot.state,
                            CrossStoreOperationState.INTERRUPTED,
                        )
                        self.assertEqual(
                            snapshot.recovery_disposition,
                            RecoveryDisposition.NO_COMMITTED_RECEIPT,
                        )
                    elif boundary is CrossStoreBoundary.AFTER_DATA_PLANE_COMMIT:
                        with self.assertRaises(CrossStoreManualReviewError):
                            coordinator.execute(command)
                        snapshot = self.journal.get(request)
                        self.assertEqual(
                            snapshot.state,
                            CrossStoreOperationState.MANUAL_REVIEW,
                        )
                        self.assertEqual(
                            snapshot.recovery_disposition,
                            RecoveryDisposition.ARTIFACT_MISMATCH,
                        )
                    elif boundary in successful_boundaries:
                        result = coordinator.execute(command)
                        self.assertTrue(result.replayed)
                        self.assertEqual(
                            self.journal.get(request).state,
                            CrossStoreOperationState.PROJECTED,
                        )
                    else:
                        self.fail(f"unclassified boundary {boundary}")

    def test_full_plw_demo_projects_37253_prepared_rows_and_replays(self) -> None:
        self._reset_control_plane()
        with tempfile.TemporaryDirectory() as root:
            database = DuckDbWorkspaceDatabase(root)
            artifacts = LocalArtifactStore(root)
            workspace_id = str(uuid4())
            WorkspaceStateRepository(database).initialize_workbench(
                WorkspaceState(
                    workspace_id=workspace_id,
                    name="PLW manufacturing local trial",
                    source_system="XLSX",
                    odoo_connection_mode=OdooConnectionMode.LOCAL,
                    odoo_base_url="http://127.0.0.1:8069",
                    odoo_database="fictional_odoo",
                    intended_models=(
                        "product.template",
                        "mrp.bom",
                        "mrp.bom.line",
                        "mrp.routing.workcenter",
                    ),
                    status=WorkspaceStatus.REGISTERED,
                    registered_at=NOW,
                ),
                actor=LOCAL_ACTOR,
            )
            request = OperationRequest(
                operation_id=str(uuid4()),
                organization_id=ORGANIZATION,
                project_id=PROJECT_ID,
                operation_kind="workspace.prepare",
                idempotency_key="prepare:plw-manufacturing:revision-1",
                semantic_input_hash=self.plw_scenario.scenario_hash,
                authorization_receipt_id="authorization-001",
                requested_by=ActorIdentity(
                    issuer="urn:impodo:test",
                    subject_id="fictional-user",
                    display_name="Fictional user",
                ),
                requested_at=NOW,
            )
            clock = _Clock()
            coordinator = CrossStoreOperationCoordinator(
                journal=self.journal,
                leases=self.leases,
                data_plane=DuckDbPreparationDataPlaneCommand(
                    database,
                    artifacts,
                    self.plw_scenario,
                    workspace_id,
                    clock=clock,
                ),
                artifacts=LocalArtifactEvidenceVerifier(artifacts),
                projections=ReceiptFactsProjectionUpdater(),
                clock=clock,
            )
            command = WorkspaceCrossStoreCommand(
                request=request,
                workspace_id=workspace_id,
                holder_attempt_id=str(uuid4()),
            )

            first = coordinator.execute(command)
            replay = coordinator.execute(command)

            self.assertFalse(first.replayed)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.data_plane_receipt, first.data_plane_receipt)
            artifact = first.data_plane_receipt.artifacts[0]
            with (
                artifacts.materialize_prepared_snapshot(
                    workspace_id,
                    artifact.storage_key,
                    expected_sha256=artifact.sha256,
                ) as path,
                duckdb.connect(":memory:") as connection,
            ):
                counts = connection.execute(
                    """
                    SELECT dataset, count(*)
                      FROM read_parquet(?)
                     GROUP BY dataset
                     ORDER BY dataset
                    """,
                    [str(path)],
                ).fetchall()
            self.assertEqual(
                counts,
                [
                    ("bom_headers", 1942),
                    ("bom_lines", 15142),
                    ("bom_operations", 17328),
                    ("products", 2841),
                ],
            )
            self.assertEqual(sum(int(row[1]) for row in counts), 37253)
            self.assertEqual(
                self.journal.get(request).state,
                CrossStoreOperationState.PROJECTED,
            )

    def _reset_control_plane(self) -> None:
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
            connection.execute(
                """
                INSERT INTO impodo_control.organization (
                    organization_id, display_name, status,
                    policy_revision, optimistic_revision, deletion_state
                ) VALUES (%s, 'Fictional organization', 'ACTIVE', 1, 1, 'NONE')
                """,
                (str(ORGANIZATION),),
            )
            connection.execute(
                """
                INSERT INTO impodo_control.project_catalogue (
                    organization_id, project_id, display_name, status,
                    data_classification, retention_days,
                    optimistic_revision, catalogue_state
                ) VALUES (
                    %s, %s, 'Fictional Project', 'DRAFT',
                    'CONFIDENTIAL', 365, 1, 'PENDING'
                )
                """,
                (str(ORGANIZATION), PROJECT_ID),
            )


if __name__ == "__main__":
    unittest.main()
