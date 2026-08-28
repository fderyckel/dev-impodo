"""Verify lean whole-artifact correction-origin contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
    CorrectionPreparedArtifact,
    CorrectionTargetIndex,
    CorrectionTargetIndexEntry,
    ProtectedCorrectionArtifactReference,
)
from impodo.domain.shared.access import ActorIdentity


IDS = tuple(f"{value:08d}-0000-4000-8000-000000000000" for value in range(1, 12))
HASHES = tuple("sha256:" + character * 64 for character in "123456789abcdef")
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
ACTOR = ActorIdentity("test", "data-manager", "Data manager")


def _index() -> CorrectionTargetIndex:
    return CorrectionTargetIndex.create(
        index_id=IDS[0],
        project_id=IDS[1],
        completed_migration_run_id=IDS[2],
        completed_workspace_id=IDS[3],
        entries=(
            CorrectionTargetIndexEntry(
                dataset="Products",
                source_row=2,
                row_id="row-2",
                target_model="product.template",
                odoo_id=702,
                completed_disposition="CREATE",
                target_binding_hash="",
            ),
            CorrectionTargetIndexEntry(
                dataset="Products",
                source_row=1,
                row_id="row-1",
                target_model="product.template",
                odoo_id=701,
                completed_disposition="UNCHANGED",
                target_binding_hash=HASHES[0],
            ),
        ),
        created_at=NOW,
    )


def _manifest(index: CorrectionTargetIndex) -> CorrectionOriginManifest:
    return CorrectionOriginManifest.create(
        manifest_id=IDS[4],
        project_id=IDS[1],
        data_version_id=IDS[5],
        completed_migration_run_id=IDS[2],
        completed_workspace_id=IDS[3],
        mapping_id=IDS[6],
        mapping_version=3,
        mapping_content_hash=HASHES[1],
        prepared_artifacts=(
            CorrectionPreparedArtifact(
                dataset_id="dataset:" + "a" * 24,
                dataset_name="Products",
                source_snapshot_hash=HASHES[2],
                logical_hash=HASHES[3],
                content_hash=HASHES[4],
                parquet_storage_key="prepared/products.parquet",
                parquet_sha256=HASHES[5],
                row_count=999,
            ),
        ),
        execution_snapshot_hash=HASHES[6],
        execution_snapshot_root_hash=HASHES[7],
        preflight_run_id=IDS[7],
        execution_run_id=IDS[8],
        execution_evidence_hash=HASHES[8],
        reconciliation_id=IDS[9],
        reconciliation_hash=HASHES[9],
        target_hash=HASHES[10],
        schema_hash=HASHES[11],
        read_context_hash=HASHES[12],
        target_observed_at="2026-08-28T04:00:00Z",
        target_index=ProtectedCorrectionArtifactReference(
            artifact_id=index.index_id,
            logical_hash=index.index_hash,
            storage_key="project/correction-target-indexes/index.ipe",
            artifact_hash=HASHES[13],
        ),
        created_by=ACTOR,
        created_at=NOW,
    )


class CorrectionOriginTests(unittest.TestCase):
    def test_whole_index_and_manifest_round_trip_without_row_hashes_or_values(self) -> None:
        index = _index()
        manifest = _manifest(index)

        self.assertEqual(
            CorrectionTargetIndex.from_protected_json(index.protected_json()),
            index,
        )
        self.assertEqual(
            CorrectionOriginManifest.from_protected_json(manifest.protected_json()),
            manifest,
        )
        payload = index.protected_json()
        self.assertNotIn(b"row_hash", payload)
        self.assertNotIn(b"previous", payload)
        self.assertNotIn(b"corrected", payload)
        self.assertEqual(manifest.prepared_artifacts[0].row_count, 999)
        self.assertNotIn("values", manifest.prepared_artifacts[0].portable_dict())

    def test_target_index_rejects_ambiguous_exact_target(self) -> None:
        entries = _index().entries
        duplicate = CorrectionTargetIndexEntry(
            dataset="Other",
            source_row=1,
            row_id="other-1",
            target_model=entries[0].target_model,
            odoo_id=entries[0].odoo_id,
            completed_disposition="UPDATE",
            target_binding_hash=HASHES[0],
        )

        with self.assertRaisesRegex(CorrectionOriginError, "targets are ambiguous"):
            CorrectionTargetIndex.create(
                index_id=IDS[10],
                project_id=IDS[1],
                completed_migration_run_id=IDS[2],
                completed_workspace_id=IDS[3],
                entries=entries + (duplicate,),
                created_at=NOW,
            )

    def test_manifest_hash_tampering_fails_closed(self) -> None:
        manifest = _manifest(_index())
        payload = json.loads(manifest.protected_json())
        payload["mapping_version"] = 4

        with self.assertRaisesRegex(CorrectionOriginError, "hash changed"):
            CorrectionOriginManifest.from_protected_json(
                json.dumps(payload).encode("utf-8")
            )


if __name__ == "__main__":
    unittest.main()
