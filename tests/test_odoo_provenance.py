from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import json
from hashlib import sha256
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid4, uuid5

from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    Capability,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.odoo_provenance_repository import (
    OdooProvenanceRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.schema_repository import SchemaRepository
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.adapters.protected_odoo_provenance import (
    ProtectedOdooProvenanceError,
)
from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.adapters.protected_odoo_comparison import ProtectedOdooComparisonCodec
from impodo.adapters.protected_odoo_provenance import ProtectedOdooProvenanceCodec
from impodo.domain.odoo_capture import (
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
)
from impodo.domain.odoo_provenance import (
    OdooCaptureManifest,
    OdooCaptureOriginHeader,
    OdooExecutionOriginBatch,
    OdooExecutionOriginManifest,
    OdooOriginBatch,
    OdooProvenanceError,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.serialization import content_hash
from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStatus,
    SourceMode,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.shared.secrets import SecretStoreError
from impodo.application.data_version.source_snapshots import SourceSnapshotCandidateWriter
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
)
from impodo.application.workspace.access import WorkspaceAccessContext, WorkspaceAccessService
from impodo.domain.workspace.errors import WorkspaceError


ROOT = Path(__file__).resolve().parents[1]
HASHES = tuple("sha256:" + digit * 64 for digit in "123456789abcdef")


def _lineage_id(kind: str, workspace_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"impodo-test:{kind}:{workspace_id}"))


def _data_version_id(workspace_id: str) -> str:
    return _lineage_id("data-version", workspace_id)


def _protected_root(root: str | Path, workspace_id: str) -> Path:
    return Path(root) / "dv" / _data_version_id(workspace_id) / "protected"


class _WorkspaceLineageRepository:
    def resolve_workspace_access_context(
        self,
        workspace_id: str,
    ) -> WorkspaceAccessContext:
        return WorkspaceAccessContext(
            project_id=_lineage_id("project", workspace_id),
            workspace_id=workspace_id,
            data_version_id=_data_version_id(workspace_id),
            migration_run_id=_lineage_id("run", workspace_id),
        )


class OdooProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(self.database)
        derived = DerivedEntityRepository(self.database)
        self.sources = SourceRepository(self.database, derived)
        self.schemas = SchemaRepository(self.database)
        self.artifacts = LocalArtifactStore(self.temporary.name)
        self.repository = OdooProvenanceRepository(
            self.database,
            self.artifacts,
            protected_root=lambda workspace_id: _protected_root(
                self.temporary.name,
                workspace_id,
            ),
        )
        self.secrets = MemorySecretStore()
        self.workspace_access = WorkspaceAccessService(
            _WorkspaceLineageRepository(),
            CapabilityAuthorizationPolicy(),
        )
        self.service = OdooProvenanceService(
            self.workspace_states,
            self.sources,
            self.repository,
            self.secrets,
            self.workspace_access,
            ProtectedOdooProvenanceCodec(),
            ProtectedOdooComparisonCodec(),
        )
        self.now = datetime.now(timezone.utc)
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Odoo contacts",
            source_system="Odoo",
            source_mode=SourceMode.ODOO,
            retention_days=1,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="production",
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
            registered_at=self.now,
            created_at=self.now,
            updated_at=self.now,
        )
        self.workspace_states.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
        schema = OdooSchemaCatalog(
            workspace_id=self.workspace_state.workspace_id,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=self.now,
            captured_by="Data Manager",
            connection_mode="REMOTE",
            database="production",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="res.partner",
                    label="Contact",
                    fields=(
                        SchemaField(
                            name="active",
                            label="Active",
                            type="boolean",
                            required=False,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                        ),
                        SchemaField(
                            name="name",
                            label="Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                        ),
                    ),
                ),
            ),
            content_hash=HASHES[1],
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=HASHES[2],
            read_principal_hash=HASHES[3],
            read_permission_hash=HASHES[4],
            read_context_hash=HASHES[5],
            connection_target_hash=HASHES[0],
        )
        self.schemas.save_odoo_schema_catalog(
            self.workspace_state.workspace_id,
            schema,
            actor=LOCAL_ACTOR,
        )
        self.selection = self._selection(version=1)
        self.sources.save_odoo_capture_selection(
            self.workspace_state.workspace_id,
            self.selection,
            actor=LOCAL_ACTOR,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_is_encrypted_batched_and_contains_no_per_row_hash(self) -> None:
        batches = self._batches()

        manifest = self._publish(batches)
        restored = self.service.current_manifest(
            self.workspace_state.workspace_id,
            actor=LOCAL_ACTOR,
        )
        decoded = self.service.read_current_origins(
            self.workspace_state.workspace_id,
            actor=LOCAL_ACTOR,
            now=self.now + timedelta(hours=1),
        )

        self.assertEqual(restored, manifest)
        self.assertIsNotNone(decoded)
        header, restored_batches = decoded or (None, ())
        self.assertEqual(header, OdooCaptureOriginHeader(high_water_id=99))
        self.assertEqual(restored_batches, batches)
        self.assertEqual(
            self.service.history(self.workspace_state.workspace_id, actor=LOCAL_ACTOR),
            (manifest,),
        )
        artifact = _protected_root(
            self.temporary.name,
            self.workspace_state.workspace_id,
        ) / manifest.provenance_storage_key
        encrypted = artifact.read_bytes()
        self.assertNotIn(self.now.date().isoformat().encode("ascii"), encrypted)
        self.assertNotIn((41).to_bytes(8, "big"), encrypted)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE((artifact.parents[1]).stat().st_mode),
                0o700,
            )
        origin_fields = {item.name for item in fields(OdooOriginBatch)}
        self.assertEqual(
            origin_fields,
            {"first_row_ordinal", "odoo_ids", "write_dates"},
        )
        self.assertFalse(any("hash" in item for item in origin_fields))

    def test_exact_contract_round_trip_rejects_added_or_tampered_fields(self) -> None:
        manifest = self._publish(self._batches())

        self.assertEqual(OdooCaptureManifest.from_json(manifest.to_json()), manifest)
        payload = json.loads(manifest.to_json())
        payload["unexpected_origin_rows"] = []
        with self.assertRaisesRegex(OdooProvenanceError, "shape"):
            OdooCaptureManifest.from_json(json.dumps(payload))
        with self.assertRaisesRegex(OdooProvenanceError, "manifest hash"):
            replace(manifest, row_count=1)

    def test_manifest_hashes_once_and_reuses_precomputed_metadata_index(self) -> None:
        selection = self.selection
        with patch(
            "impodo.domain.odoo_provenance.content_hash",
            wraps=content_hash,
        ) as hash_manifest:
            manifest = OdooCaptureManifest.create(
                manifest_id=str(uuid4()),
                selection=selection,
                dataset_id=selection.dataset_id,
                column_stable_keys=selection.column_stable_keys,
                row_count=2,
                data_logical_hash=HASHES[6],
                data_sha256=HASHES[7],
                data_storage_key="snapshots/values.parquet",
                data_size_bytes=123,
                provenance_logical_hash=HASHES[8],
                provenance_sha256=HASHES[9],
                provenance_storage_key="captures/origins.iprv",
                provenance_size_bytes=456,
                capture_started_at=self.now,
                capture_finished_at=self.now + timedelta(minutes=1),
                retention_until=self.now + timedelta(days=1),
                created_by="issuer:subject",
            )

        self.assertEqual(hash_manifest.call_count, 1)
        self.assertEqual(manifest.column_stable_keys, selection.column_stable_keys)

    def test_hash_count_is_artifact_level_not_row_level(self) -> None:
        with (
            patch(
                "impodo.adapters.protected_odoo_provenance.sha256",
                wraps=sha256,
            ) as stream_hash,
            patch(
                "impodo.adapters.duckdb.odoo_provenance_repository.sha256",
                wraps=sha256,
            ) as repository_verification,
        ):
            self._publish(self._batches())

        # One logical payload root, one ciphertext root, and one required
        # repository trust-boundary verification regardless of row count.
        self.assertEqual(stream_hash.call_count, 2)
        self.assertEqual(repository_verification.call_count, 1)

    def test_tamper_wrong_key_and_binding_fail_closed(self) -> None:
        manifest = self._publish(self._batches())
        artifact = _protected_root(
            self.temporary.name,
            self.workspace_state.workspace_id,
        ) / manifest.provenance_storage_key
        original = artifact.read_bytes()
        changed = bytearray(original)
        changed[-1] ^= 1
        artifact.write_bytes(changed)

        with self.assertRaisesRegex(
            ProtectedOdooProvenanceError,
            "artifact hash verification",
        ):
            self.service.read_current_origins(
                self.workspace_state.workspace_id,
                actor=LOCAL_ACTOR,
                now=self.now + timedelta(hours=1),
            )

        artifact.write_bytes(original)
        key_id = next(iter(self.secrets.values))
        self.secrets.values[key_id] = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="
        with self.assertRaisesRegex(
            ProtectedOdooProvenanceError,
            "authentication failed",
        ):
            self.service.read_current_origins(
                self.workspace_state.workspace_id,
                actor=LOCAL_ACTOR,
                now=self.now + timedelta(hours=1),
            )

    def test_quota_failure_preserves_last_valid_pointer_and_cleans_candidate(
        self,
    ) -> None:
        first = self._publish(self._batches())
        constrained = OdooProvenanceRepository(
            self.database,
            self.artifacts,
            history_quota_bytes=(first.data_size_bytes + first.provenance_size_bytes),
            protected_root=lambda workspace_id: _protected_root(
                self.temporary.name,
                workspace_id,
            ),
        )
        with self.assertRaisesRegex(WorkspaceError, "history quota"):
            self._publish(self._batches(), repository=constrained)

        self.assertEqual(self.repository.get_current(self.workspace_state.workspace_id), first)
        self.assertEqual(
            self.repository.history(self.workspace_state.workspace_id),
            (first,),
        )
        candidates = _protected_root(
            self.temporary.name,
            self.workspace_state.workspace_id,
        ) / "candidates"
        self.assertEqual(tuple(candidates.glob("*")), ())

    def test_selection_change_invalidates_current_but_retains_history(self) -> None:
        manifest = self._publish(self._batches())
        replacement = self._selection(
            version=2,
            selection_id=self.selection.selection_id,
        )

        self.sources.save_odoo_capture_selection(
            self.workspace_state.workspace_id,
            replacement,
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(self.repository.get_current(self.workspace_state.workspace_id))
        self.assertEqual(self.repository.history(self.workspace_state.workspace_id), (manifest,))

    def test_target_change_invalidates_current_but_retains_history(self) -> None:
        manifest = self._publish(self._batches())
        changed = replace(
            self.workspace_state,
            odoo_database="replacement",
            revision=self.workspace_state.revision + 1,
            updated_at=self.now + timedelta(minutes=2),
        )

        self.workspace_states.save(
            changed,
            expected_revision=self.workspace_state.revision,
            event_type="WORKSPACE_TARGET_UPDATED",
            event_detail="replacement target",
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(self.repository.get_current(self.workspace_state.workspace_id))
        self.assertEqual(self.repository.history(self.workspace_state.workspace_id), (manifest,))

    def test_retention_refuses_expired_reads_and_purges_expired_history(self) -> None:
        manifest = self._publish(self._batches())
        expired_at = self.now + timedelta(days=2)

        with self.assertRaisesRegex(WorkspaceError, "expired"):
            self.service.read_current_origins(
                self.workspace_state.workspace_id,
                actor=LOCAL_ACTOR,
                now=expired_at,
            )
        self.assertEqual(
            self.service.enforce_retention(
                self.workspace_state.workspace_id,
                actor=LOCAL_ACTOR,
                now=expired_at,
            ),
            1,
        )
        self.assertIsNone(self.repository.get_current(self.workspace_state.workspace_id))
        self.assertEqual(self.repository.history(self.workspace_state.workspace_id), ())
        artifact = _protected_root(
            self.temporary.name,
            self.workspace_state.workspace_id,
        ) / manifest.provenance_storage_key
        self.assertFalse(artifact.exists())
        self.assertFalse(
            (
                Path(self.temporary.name)
                / "dv"
                / _data_version_id(self.workspace_state.workspace_id)
                / manifest.data_storage_key
            ).exists()
        )
        self.assertIsNone(self.sources.get_source_selection(self.workspace_state.workspace_id))

    def test_purge_preserves_a_value_artifact_reused_by_retained_history(self) -> None:
        first = self._publish(self._batches())
        self.repository.invalidate_current(
            self.workspace_state.workspace_id,
            reason="RECAPTURE",
            actor=LOCAL_ACTOR,
        )
        unique_artifact_quota = OdooProvenanceRepository(
            self.database,
            self.artifacts,
            history_quota_bytes=(
                first.data_size_bytes + 2 * first.provenance_size_bytes
            ),
            protected_root=lambda workspace_id: _protected_root(
                self.temporary.name,
                workspace_id,
            ),
        )
        second = self._publish(
            self._batches(),
            repository=unique_artifact_quota,
            capture_finished_at=self.now + timedelta(days=1),
        )
        self.assertEqual(first.data_storage_key, second.data_storage_key)
        self.repository.invalidate_current(
            self.workspace_state.workspace_id,
            reason="TEST_HISTORY",
            actor=LOCAL_ACTOR,
        )

        purged = self.repository.purge_expired_history(
            self.workspace_state.workspace_id,
            now=self.now + timedelta(days=1, minutes=2),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(purged, 1)
        self.assertEqual(self.repository.history(self.workspace_state.workspace_id), (second,))
        self.assertTrue(
            (
                Path(self.temporary.name)
                / "dv"
                / _data_version_id(self.workspace_state.workspace_id)
                / second.data_storage_key
            ).is_file()
        )

    def test_authorization_is_checked_before_protected_repository_access(self) -> None:
        unauthorized = Actor(
            identity=ActorIdentity(
                issuer="test",
                subject_id="viewer",
                display_name="Viewer",
            ),
            capabilities=frozenset({Capability.PROJECT_VIEW}),
        )
        with self.assertRaises(PermissionError):
            self.service.current_manifest(
                self.workspace_state.workspace_id,
                actor=unauthorized,
            )

    def test_project_key_deletion_makes_retained_ciphertext_unreadable(self) -> None:
        self._publish(self._batches())
        self.assertTrue(self.secrets.values)

        self.service.delete_recipe_workspace_key(
            self.workspace_state.workspace_id,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(self.secrets.values, {})
        with self.assertRaisesRegex(SecretStoreError, "key is missing"):
            self.service.read_current_origins(
                self.workspace_state.workspace_id,
                actor=LOCAL_ACTOR,
                now=self.now + timedelta(hours=1),
            )

    def test_execution_origin_reuses_snapshot_hashes_and_source_ordinals(self) -> None:
        batch = OdooExecutionOriginBatch(
            execution_row_hashes=(HASHES[0], HASHES[1]),
            source_row_ordinals=(1, 2),
        )
        manifest = OdooExecutionOriginManifest.create(
            origin_id=str(uuid4()),
            workspace_id=self.workspace_state.workspace_id,
            capture_manifest_hash=HASHES[2],
            execution_snapshot_hash=HASHES[3],
            connection_target_hash=HASHES[4],
            write_principal_hash=HASHES[5],
            write_permission_hash=HASHES[6],
            context_hash=HASHES[7],
            row_count=2,
            logical_hash=HASHES[8],
            artifact_sha256=HASHES[9],
            storage_key="captures/execution.iprv",
            size_bytes=100,
            created_at=self.now,
            created_by="issuer:writer",
        )

        self.assertEqual(batch.source_row_ordinals, (1, 2))
        self.assertEqual(
            OdooExecutionOriginManifest.from_json(manifest.to_json()),
            manifest,
        )
        self.assertNotIn("odoo_id", manifest.to_json())

    def _publish(
        self,
        batches: tuple[OdooOriginBatch, ...],
        *,
        repository: OdooProvenanceRepository | None = None,
        capture_finished_at: datetime | None = None,
    ) -> OdooCaptureManifest:
        row_count = sum(batch.row_count for batch in batches)
        finished_at = capture_finished_at or self.now + timedelta(minutes=1)
        columns = (
            SourceDatasetColumn(
                1, "active", self.selection.column_stable_keys[0], "boolean"
            ),
            SourceDatasetColumn(
                2, "name", self.selection.column_stable_keys[1], "string"
            ),
        )
        schema = SourceSnapshotSchema.create(
            SourceSnapshotColumn.create(
                ordinal=item.ordinal,
                stable_key=item.stable_key,
                source_name=item.source_name,
                candidate_type=item.candidate_type,
            )
            for item in columns
        )
        current = self.sources.get_source_selection(self.workspace_state.workspace_id)
        version = current.version + 1 if current else 1
        dataset = SourceDataset(
            dataset_id=self.selection.dataset_id,
            name=self.selection.dataset_name,
            source=self.selection.source_binding,
            row_count=row_count,
            columns=columns,
        )
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=version,
            data_version_id=_data_version_id(self.workspace_state.workspace_id),
            created_at=finished_at,
            created_by=LOCAL_ACTOR.identity.display_name,
            datasets=(dataset,),
            content_hash=content_hash(
                {
                    "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
                    "data_version_id": _data_version_id(self.workspace_state.workspace_id),
                    "version": version,
                    "datasets": [dataset.to_dict()],
                }
            ),
        )
        with self.artifacts.prepare_source_snapshot(
            _data_version_id(self.workspace_state.workspace_id)
        ) as workspace:
            writer = SourceSnapshotCandidateWriter(
                workspace,
                schema,
                batch_rows=500,
            )
            for batch in batches:
                writer.append_columnar_page(
                    first_row_ordinal=batch.first_row_ordinal,
                    values_by_name={
                        "active": tuple(True for _ in batch.odoo_ids),
                        "name": tuple(f"Name {item}" for item in batch.odoo_ids),
                    },
                )
            candidate = writer.finalize()
            snapshot = SourceSnapshot.create(
                data_version_id=_data_version_id(self.workspace_state.workspace_id),
                dataset_id=dataset.dataset_id,
                dataset_name=dataset.name,
                source=dataset.source,
                physical_selection_hash=selection.content_hash,
                schema=schema,
                row_count=row_count,
                data_logical_hash=candidate.data_logical_hash,
                parquet_sha256=candidate.parquet_sha256,
                created_at=selection.created_at,
            )
            self.artifacts.publish_source_snapshot(
                _data_version_id(self.workspace_state.workspace_id),
                candidate.path,
                snapshot.parquet_storage_key,
                expected_sha256=candidate.parquet_sha256,
            )
        protected = self.service.prepare_capture_origins(
            self.workspace_state.workspace_id,
            actor=LOCAL_ACTOR,
            header=OdooCaptureOriginHeader(high_water_id=99),
            batches=batches,
            row_count=row_count,
            data_logical_hash=candidate.data_logical_hash,
            data_sha256=candidate.parquet_sha256,
            data_storage_key=snapshot.parquet_storage_key,
            data_size_bytes=candidate.size_bytes,
            capture_started_at=finished_at - timedelta(minutes=1),
            capture_finished_at=finished_at,
        )
        publisher = repository or self.repository
        try:
            publisher.publish_complete_capture(
                self.workspace_state.workspace_id,
                protected.manifest,
                protected.encrypted_bytes,
                selection,
                snapshot,
                actor=LOCAL_ACTOR,
            )
        except Exception:
            publisher.recover_incomplete_publications(self.workspace_state.workspace_id)
            raise
        return protected.manifest

    def _selection(
        self,
        *,
        version: int,
        selection_id: str | None = None,
    ) -> OdooCaptureSelection:
        return OdooCaptureSelection.create(
            selection_id=selection_id or str(uuid4()),
            version=version,
            data_version_id=_data_version_id(self.workspace_state.workspace_id),
            dataset_name="odoo_contacts",
            model="res.partner",
            field_names=("active", "name"),
            filter_policy=OdooCaptureFilterPolicy.ACTIVE_RECORDS,
            max_rows=1_000,
            connection_target_hash=HASHES[0],
            schema_scope_hash=HASHES[1],
            read_principal_hash=HASHES[3],
            read_permission_hash=HASHES[4],
            context_hash=HASHES[5],
            created_at=self.now,
            created_by="Data Manager",
        )

    def _batches(self) -> tuple[OdooOriginBatch, ...]:
        return (
            OdooOriginBatch(
                first_row_ordinal=1,
                odoo_ids=(41, 42),
                write_dates=(self.now, self.now + timedelta(seconds=1)),
            ),
            OdooOriginBatch(
                first_row_ordinal=3,
                odoo_ids=(99,),
                write_dates=(None,),
            ),
        )


if __name__ == "__main__":
    unittest.main()
