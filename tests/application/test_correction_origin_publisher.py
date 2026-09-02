"""Verify lean origin publication from completed execution evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from impodo.application.correction_orchestration import (
    CorrectionOriginPublisher,
    CorrectionOriginRequest,
)
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.shared.access import Actor, ActorIdentity
from impodo.domain.shared.models import target_record_binding_hash
from impodo.domain.correction_origin import CorrectionOriginError


IDS = tuple(f"{value:08d}-0000-4000-8000-000000000000" for value in range(1, 12))
HASHES = tuple("sha256:" + character * 64 for character in "123456789abcdef")
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
ACTOR = Actor(
    ActorIdentity("test", "data-manager", "Data manager"),
    frozenset(),
)
DATASET_ID = "dataset:" + "a" * 24


class _Protected:
    def __init__(self) -> None:
        self.index = None
        self.manifest = None

    def put_target_index(self, index):
        self.index = index
        return SimpleNamespace(
            project_id=index.project_id,
            index_id=index.index_id,
            index_hash=index.index_hash,
            storage_key="project/correction-target-indexes/index.ipe",
            artifact_hash=HASHES[12],
        )

    def put_origin(self, manifest):
        self.manifest = manifest
        return SimpleNamespace(
            project_id=manifest.project_id,
            manifest_id=manifest.manifest_id,
            manifest_hash=manifest.manifest_hash,
            storage_key="project/correction-origins/origin.ipe",
            artifact_hash=HASHES[13],
        )


class _Bindings:
    def __init__(self) -> None:
        self.candidate = None

    def seal_completed_origin(self, binding, **values):
        self.candidate = binding
        self.expected = (
            values["expected_run_revision"],
            values["expected_workspace_revision"],
        )
        return binding


class CorrectionOriginPublisherTests(unittest.TestCase):
    def test_publisher_references_prepared_parquet_without_copying_values(self) -> None:
        workspace_id = IDS[3]
        run_id = IDS[2]
        target_binding = target_record_binding_hash("product.template", 701)
        snapshot = SimpleNamespace(
            workspace_id=workspace_id,
            mapping_id=IDS[4],
            mapping_version=2,
            mapping_content_hash=HASHES[0],
            semantic_hash=HASHES[1],
            root_hash=HASHES[2],
            preflight_run_id=IDS[5],
            target_hash=HASHES[3],
            target_database="fixture",
            target_snapshot_at="2026-08-28T03:59:00Z",
            read_context_hash=HASHES[4],
            record_snapshot_hash=HASHES[5],
            datasets=(
                SimpleNamespace(dataset="Products"),
            ),
            rows=(
                SimpleNamespace(
                    row_id="row-1",
                    dataset="Products",
                    source_row=1,
                    target_model="product.template",
                    disposition="UPDATE",
                    target_binding_hash=target_binding,
                ),
            ),
        )
        execution = ExecutionRun(
            run_id=IDS[6],
            workspace_id=workspace_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            batch_rows=50,
            status=ExecutionRunStatus.COMPLETED,
            started_at=NOW,
            started_by="Data manager",
            completed_at=NOW,
            rows=(
                ExecutionRowAttempt(
                    row_id="row-1",
                    dataset="Products",
                    source_row=1,
                    target_model="product.template",
                    operation="UPDATE",
                    field_names=("active",),
                    proposed_external_id="",
                    status=ExecutionRowStatus.COMMITTED,
                    attempt=1,
                    odoo_id=701,
                ),
            ),
        )
        reconciliation = ReconciliationRun(
            reconciliation_id=IDS[7],
            workspace_id=workspace_id,
            execution_run_id=execution.run_id,
            snapshot_hash=snapshot.semantic_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=ReconciliationRunStatus.VERIFIED,
            verified_at=NOW,
            verified_by="Data manager",
            unchanged_count=0,
            rows=(
                ReconciliationRow(
                    row_id="row-1",
                    dataset="Products",
                    source_row=1,
                    target_model="product.template",
                    operation="UPDATE",
                    execution_status="COMMITTED",
                    status=ReconciliationRowStatus.VERIFIED,
                    odoo_id=701,
                ),
            ),
        )
        prepared = PreparedSnapshot.create(
            workspace_id=workspace_id,
            dataset_id=DATASET_ID,
            dataset_name="Products",
            source_snapshot_hash=HASHES[6],
            mapping_hash=snapshot.mapping_content_hash,
            schema_hash=HASHES[7],
            transformation_program_hash=HASHES[8],
            row_count=1,
            physical_schema_hash=HASHES[9],
            parquet_sha256=HASHES[10],
            created_at=NOW,
        )
        protected = _Protected()
        bindings = _Bindings()
        request = CorrectionOriginRequest(
            completed_run=SimpleNamespace(
                migration_run_id=run_id,
                project_id=IDS[1],
                data_version_id=IDS[8],
                purpose=MigrationRunPurpose.AUTHORING,
                optimistic_revision=4,
            ),
            completed_workspace=SimpleNamespace(
                workspace_id=workspace_id,
                project_id=IDS[1],
                data_version_id=IDS[8],
                migration_run_id=run_id,
                optimistic_revision=6,
            ),
            mapping=SimpleNamespace(
                mapping_id=snapshot.mapping_id,
                version=snapshot.mapping_version,
                definition=SimpleNamespace(
                    content_hash=snapshot.mapping_content_hash
                ),
            ),
            prepared_snapshots=(prepared,),
            execution_snapshot=snapshot,
            execution=execution,
            reconciliation=reconciliation,
            target_records=SimpleNamespace(
                fingerprint=SimpleNamespace(target_hash=snapshot.target_hash),
                content_hash=snapshot.record_snapshot_hash,
                complete=True,
                records={"product.template": (SimpleNamespace(odoo_id=701),)},
            ),
        )
        publisher = CorrectionOriginPublisher(bindings, protected)
        publication = publisher.publish(
            request,
            actor=ACTOR,
        )

        self.assertEqual(bindings.expected, (4, 6))
        self.assertEqual(publication.target_index.entries[0].odoo_id, 701)
        artifact = publication.manifest.prepared_artifacts[0]
        self.assertEqual(artifact.parquet_storage_key, prepared.parquet_storage_key)
        self.assertEqual(artifact.parquet_sha256, prepared.parquet_sha256)
        self.assertFalse(hasattr(artifact, "values"))
        self.assertNotIn(b"row_hash", publication.target_index.protected_json())

        with self.assertRaises(CorrectionOriginError) as missing:
            publisher.publish(
                replace(request, prepared_snapshots=()),
                actor=ACTOR,
            )
        self.assertEqual(
            missing.exception.failure_code,
            "CORRECTION_ORIGIN_PREPARED_MISSING",
        )

        with self.assertRaises(CorrectionOriginError) as dataset_mismatch:
            publisher.publish(
                replace(
                    request,
                    prepared_snapshots=(
                        PreparedSnapshot.create(
                            workspace_id=workspace_id,
                            dataset_id=DATASET_ID,
                            dataset_name="Other dataset",
                            source_snapshot_hash=HASHES[6],
                            mapping_hash=snapshot.mapping_content_hash,
                            schema_hash=HASHES[7],
                            transformation_program_hash=HASHES[8],
                            row_count=1,
                            physical_schema_hash=HASHES[9],
                            parquet_sha256=HASHES[10],
                            created_at=NOW,
                        ),
                    ),
                ),
                actor=ACTOR,
            )
        self.assertEqual(
            dataset_mismatch.exception.failure_code,
            "CORRECTION_ORIGIN_DATASET_SET_MISMATCH",
        )


if __name__ == "__main__":
    unittest.main()
