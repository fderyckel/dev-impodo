from __future__ import annotations

from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.schema_repository import SchemaRepository
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.application.odoo_capture_publication_service import (
    OdooCapturePublicationService,
)
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.odoo_source_capture_service import OdooSourceCaptureService
from impodo.application.bounded_preparation import prepare_bounded_direct_session
from impodo.application.preparation_service import (
    _verify_odoo_preparation_evidence,
    canonical_source_hashes,
)
from impodo.artifacts import ArtifactSizeError, LocalArtifactStore
from impodo.connectors import MetadataSnapshot
from impodo.domain.odoo_capture import OdooCaptureFilterPolicy, OdooCaptureSelection
from impodo.domain.odoo_source_capture import (
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooSourceCaptureCancelled,
    OdooCaptureValueColumn,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    MappingTargetMode,
    ScalarFieldMapping,
)
from impodo.odoo_capture_jobs import OdooCapturePhase
from impodo.domain.source_snapshot import SourceSnapshotColumn, SourceSnapshotSchema
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    ProtectedOdooReadContext,
    TargetFingerprint,
)
from impodo.projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectStatus,
    SourceMode,
)
from impodo.secrets import MemorySecretStore
from impodo.source_snapshot_io import (
    SourceSnapshotCandidateWriter,
    load_source_snapshot_table,
)
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)


HASHES = tuple("sha256:" + digit * 64 for digit in "123456789")
DiskUsage = namedtuple("DiskUsage", "total used free")


class OdooCapturePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = DuckDbDatabase(self.temporary.name)
        self.artifacts = LocalArtifactStore(self.temporary.name)
        self.projects = ProjectRepository(self.database)
        self.sources = SourceRepository(
            self.database,
            DerivedEntityRepository(self.database),
        )
        self.schemas = SchemaRepository(self.database)
        self.repository = OdooProvenanceRepository(
            self.database,
            self.artifacts,
        )
        self.secrets = MemorySecretStore()
        self.now = datetime.now(timezone.utc)
        self.project = _project(self.now)
        self.projects.create(self.project, actor=LOCAL_ACTOR)
        self.schema = _schema(self.project.project_id, self.now)
        self.schemas.save_odoo_schema_catalog(
            self.project.project_id,
            self.schema,
            actor=LOCAL_ACTOR,
        )
        self.capture_selection = _selection(
            self.project.project_id,
            self.schema,
            self.now,
        )
        self.sources.save_odoo_capture_selection(
            self.project.project_id,
            self.capture_selection,
            actor=LOCAL_ACTOR,
        )
        authorization = CapabilityAuthorizationPolicy()
        self.provenance = OdooProvenanceService(
            self.projects,
            self.sources,
            self.repository,
            self.secrets,
            authorization,
        )
        self.service = OdooCapturePublicationService(
            OdooSourceCaptureService(
                self.projects,
                self.sources,
                self.schemas,
                authorization,
            ),
            self.sources,
            self.provenance,
            self.repository,
            self.artifacts,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_streams_values_once_and_promotes_all_current_roots(self) -> None:
        gateway = _Gateway(self.schema, self.now)
        from impodo.domain.source_snapshot import EncodedSourceCell

        with patch.object(
            EncodedSourceCell,
            "from_python",
            wraps=EncodedSourceCell.from_python,
        ) as encode_cell:
            publication = self.service.publish(
                self.project.project_id,
                gateway,
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(encode_cell.call_count, 2)
        self.assertEqual(
            gateway.calls, ["identity", "schema", "open", "identity", "schema"]
        )
        self.assertEqual(
            publication.manifest.data_logical_hash,
            publication.source_snapshot.data_logical_hash,
        )
        self.assertEqual(
            publication.manifest.data_sha256,
            publication.source_snapshot.parquet_sha256,
        )
        self.assertEqual(
            self.repository.get_current(self.project.project_id),
            publication.manifest,
        )
        self.assertEqual(
            self.sources.get_source_selection(self.project.project_id),
            publication.source_selection,
        )
        self.assertEqual(
            self.sources.get_current_source_snapshots(self.project.project_id),
            (publication.source_snapshot,),
        )
        with self.artifacts.materialize_source_snapshot(
            self.project.project_id,
            publication.source_snapshot.parquet_storage_key,
            expected_sha256=publication.source_snapshot.parquet_sha256,
        ) as path:
            table = load_source_snapshot_table(path, publication.source_snapshot)
        self.assertEqual(
            tuple(row.values["name"] for row in table.rows),
            ("Alice", "Bob"),
        )
        origins = self.provenance.read_current_origins(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            now=self.now + timedelta(hours=1),
        )
        self.assertIsNotNone(origins)
        self.assertEqual((origins or (None, ()))[1][0].odoo_ids, (41, 42))

    def test_pinned_capture_prepares_offline_without_portable_ids(self) -> None:
        gateway = _Gateway(self.schema, self.now)
        publication = self.service.publish(
            self.project.project_id,
            gateway,
            actor=LOCAL_ACTOR,
        )
        calls_after_capture = tuple(gateway.calls)
        _verify_odoo_preparation_evidence(
            self.project.project_id,
            publication.source_selection,
            (publication.source_snapshot,),
            self.provenance,
            actor=LOCAL_ACTOR,
        )
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=publication.source_selection.content_hash,
            schema_hash=self.schema.content_hash,
            datasets=(
                DatasetMapping(
                    dataset_id=publication.source_selection.datasets[0].dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.ODOO_PINNED_UPDATE,
                    fields=(
                        ScalarFieldMapping(
                            target_field="name",
                            source_column_key=(
                                publication.source_selection.datasets[0]
                                .columns[0]
                                .stable_key
                            ),
                        ),
                    ),
                    approved_write_fields=("name",),
                ),
            ),
        )

        bounded = prepare_bounded_direct_session(
            self.project,
            definition,
            1,
            publication.source_selection,
            publication.source_selection,
            (),
            self.artifacts,
            None,
            PreparationSessionRepository(self.database, self.artifacts),
            actor=LOCAL_ACTOR,
            source_snapshots=(publication.source_snapshot,),
        )

        self.assertEqual(tuple(gateway.calls), calls_after_capture)
        self.assertEqual(len(bounded.run.rows), 2)
        self.assertEqual(
            tuple(item.disposition.value for item in bounded.run.rows),
            ("CANDIDATE", "CANDIDATE"),
            tuple(item.issues for item in bounded.run.rows),
        )
        self.assertEqual(
            tuple(item.issues for item in bounded.run.rows),
            ((), ()),
        )
        self.assertEqual(
            canonical_source_hashes(publication.source_selection),
            {
                publication.source_selection.datasets[0].name:
                    publication.source_selection.datasets[0].source_evidence_hash
            },
        )
        portable_rows = repr(
            tuple(item.to_portable_dict() for item in bounded.run.rows)
        )
        self.assertNotIn("odoo_ids", portable_rows)
        self.assertNotIn("'id': 41", portable_rows)
        self.assertNotIn("'id': 42", portable_rows)

    def test_disk_preflight_fails_before_any_target_call(self) -> None:
        gateway = _Gateway(self.schema, self.now)
        with patch(
            "impodo.artifacts.shutil.disk_usage",
            return_value=DiskUsage(total=100, used=100, free=0),
        ):
            with self.assertRaises(ArtifactSizeError):
                self.service.publish(
                    self.project.project_id,
                    gateway,
                    actor=LOCAL_ACTOR,
                )

        self.assertEqual(gateway.calls, [])
        self.assertIsNone(self.repository.get_current(self.project.project_id))
        self.assertIsNone(self.sources.get_source_selection(self.project.project_id))

    def test_progress_reuses_stream_accounting_without_an_extra_read(self) -> None:
        gateway = _Gateway(self.schema, self.now)
        updates = []

        publication = self.service.publish(
            self.project.project_id,
            gateway,
            actor=LOCAL_ACTOR,
            progress=updates.append,
        )

        self.assertEqual(
            tuple(update.phase for update in updates),
            (
                OdooCapturePhase.VERIFYING,
                OdooCapturePhase.READING,
                OdooCapturePhase.FINALIZING,
                OdooCapturePhase.PUBLISHING,
            ),
        )
        self.assertEqual(updates[1].completed_rows, 2)
        self.assertEqual(updates[1].page_count, 1)
        self.assertEqual(updates[1].response_bytes, 100)
        self.assertEqual(updates[-1].response_bytes, 102)
        self.assertEqual(publication.page_count, 1)
        self.assertEqual(
            gateway.calls, ["identity", "schema", "open", "identity", "schema"]
        )

    def test_logical_value_root_is_page_size_invariant_for_tier_one_values(
        self,
    ) -> None:
        names = ("boolean", "text", "integer", "date", "datetime", "selection")
        schema = SourceSnapshotSchema.create(
            SourceSnapshotColumn.create(
                ordinal=index,
                stable_key=f"column:{index}",
                source_name=name,
                candidate_type=name,
            )
            for index, name in enumerate(names, start=1)
        )
        values = {
            "boolean": (False, None),
            "text": ("", "Café 東京 😀"),
            "integer": (0, 42),
            "date": (date(2026, 8, 12), None),
            "datetime": (self.now, self.now + timedelta(seconds=1)),
            "selection": ("draft", "done"),
        }
        roots = []
        for split in (False, True):
            workspace = Path(self.temporary.name) / f"page-shape-{split}"
            workspace.mkdir()
            writer = SourceSnapshotCandidateWriter(
                workspace,
                schema,
                batch_rows=500,
            )
            if split:
                for index in range(2):
                    writer.append_columnar_page(
                        first_row_ordinal=index + 1,
                        values_by_name={
                            name: (column[index],) for name, column in values.items()
                        },
                    )
            else:
                writer.append_columnar_page(
                    first_row_ordinal=1,
                    values_by_name=values,
                )
            roots.append(writer.finalize().data_logical_hash)

        self.assertEqual(roots[0], roots[1])

    def test_failed_transaction_keeps_previous_roots_and_removes_orphans(self) -> None:
        first = self.service.publish(
            self.project.project_id,
            _Gateway(self.schema, self.now),
            actor=LOCAL_ACTOR,
        )
        before_files = tuple(
            sorted(
                path.relative_to(self.temporary.name)
                for path in Path(self.temporary.name).rglob("*.parquet")
            )
        )
        with patch.object(
            self.database,
            "_insert_workspace_audit",
            side_effect=RuntimeError("forced publication failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced publication"):
                self.service.publish(
                    self.project.project_id,
                    _Gateway(self.schema, self.now + timedelta(minutes=2)),
                    actor=LOCAL_ACTOR,
                )

        self.assertEqual(
            self.repository.get_current(self.project.project_id),
            first.manifest,
        )
        self.assertEqual(
            self.sources.get_source_selection(self.project.project_id),
            first.source_selection,
        )
        self.assertEqual(
            tuple(
                sorted(
                    path.relative_to(self.temporary.name)
                    for path in Path(self.temporary.name).rglob("*.parquet")
                )
            ),
            before_files,
        )
        candidates = (
            Path(self.temporary.name)
            / self.project.project_id
            / "protected"
            / "candidates"
        )
        self.assertEqual(tuple(candidates.glob("*")), ())

    def test_cancellation_after_a_page_publishes_nothing(self) -> None:
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        with self.assertRaises(OdooSourceCaptureCancelled):
            self.service.publish(
                self.project.project_id,
                _Gateway(self.schema, self.now),
                actor=LOCAL_ACTOR,
                cancellation=cancelled,
            )

        self.assertIsNone(self.repository.get_current(self.project.project_id))
        self.assertIsNone(self.sources.get_source_selection(self.project.project_id))
        self.assertEqual(
            tuple(Path(self.temporary.name).rglob("*.parquet")),
            (),
        )


class _Gateway:
    def __init__(self, schema: OdooSchemaCatalog, now: datetime) -> None:
        self.schema = schema
        self.now = now
        self.calls: list[str] = []
        self.context = ProtectedOdooReadContext(
            language="en_US",
            timezone="UTC",
            primary_company_id=1,
            allowed_company_ids=(1,),
        )

    def probe_identity(self, request, *, cancellation=None):
        self.calls.append("identity")
        return (
            OdooReadIdentity(
                target_hash=self.schema.connection_target_hash,
                principal_hash=self.schema.read_principal_hash,
                permission_hash=self.schema.read_permission_hash,
                context_hash=self.schema.read_context_hash,
                readable_models=("res.partner",),
                observed_at=self.now.isoformat(),
            ),
            self.context,
        )

    def probe_schema(self, request, context, *, cancellation=None):
        self.calls.append("schema")
        model = self.schema.models[0]
        return MetadataSnapshot(
            fingerprint=TargetFingerprint(
                target_hash=self.schema.connection_target_hash,
                connection_mode="REMOTE",
                database="production",
                odoo_version="19.0",
                snapshot_timestamp=self.now.isoformat(),
            ),
            models={
                model.name: ModelMetadata(
                    model=model.name,
                    description=model.label,
                    fields={
                        field.name: FieldMetadata(
                            name=field.name,
                            type=field.type,
                            label=field.label,
                            required=field.required,
                            readonly=field.readonly,
                            relation=field.relation,
                            relation_field=field.relation_field,
                            selection=field.selection,
                            stored=field.stored,
                            computed=field.computed,
                            has_inverse=field.has_inverse,
                            related=field.related,
                            translated=field.translated,
                            company_dependent=field.company_dependent,
                            searchable=field.searchable,
                            sortable=field.sortable,
                            exportable=field.exportable,
                            digits=field.digits,
                            currency_field=field.currency_field,
                        )
                        for field in model.fields
                    },
                )
            },
        )

    def open_capture(self, request, context, *, cancellation=None):
        self.calls.append("open")
        return _Session(request, self.now)

    def sample(self, request, context, *, limit, cancellation=None):
        raise AssertionError("Publication never samples live records")


class _Session:
    def __init__(self, request, now: datetime) -> None:
        self.page = OdooCapturePage(
            first_row_ordinal=1,
            odoo_ids=(41, 42),
            write_dates=(now, now + timedelta(seconds=1)),
            columns=(
                OdooCaptureValueColumn(
                    field_name="name",
                    field_type="char",
                    values=("Alice", "Bob"),
                ),
            ),
            response_bytes=100,
            normalized_bytes=20,
        )
        self._accounting = OdooCaptureAccounting(
            high_water_id=42,
            row_count=2,
            page_count=1,
            record_request_count=2,
            response_bytes=102,
            normalized_bytes=20,
            capture_started_at=now,
            capture_finished_at=now + timedelta(seconds=2),
            consistency=request.consistency,
            target_instance_assurance=request.target_instance_assurance,
            consistency_limitation="Native pages are not one database snapshot.",
        )

    def pages(self):
        return iter((self.page,))

    @property
    def accounting(self):
        return self._accounting


def _project(now: datetime) -> MigrationProject:
    return MigrationProject(
        project_id=str(uuid4()),
        name="Odoo contacts",
        source_system="Odoo",
        source_mode=SourceMode.ODOO,
        data_manager="Data Manager",
        functional_owner="Functional Owner",
        business_unit="Example",
        retention_days=1,
        odoo_connection_mode=OdooConnectionMode.REMOTE,
        odoo_base_url="https://odoo.example.test",
        odoo_database="production",
        intended_models=("res.partner",),
        status=ProjectStatus.REGISTERED,
        registered_at=now,
        created_at=now,
        updated_at=now,
    )


def _schema(project_id: str, now: datetime) -> OdooSchemaCatalog:
    eligibility = dict(
        relation=None,
        relation_field=None,
        selection=(),
        stored=True,
        computed=False,
        has_inverse=False,
        related=False,
        translated=False,
        company_dependent=False,
        searchable=True,
        sortable=True,
        exportable=True,
    )
    return OdooSchemaCatalog(
        project_id=project_id,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=now,
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
                        name="name",
                        label="Name",
                        type="char",
                        required=True,
                        readonly=False,
                        **eligibility,
                    ),
                    SchemaField(
                        name="write_date",
                        label="Last updated",
                        type="datetime",
                        required=False,
                        readonly=True,
                        **eligibility,
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


def _selection(
    project_id: str,
    schema: OdooSchemaCatalog,
    now: datetime,
) -> OdooCaptureSelection:
    return OdooCaptureSelection.create(
        selection_id=str(uuid4()),
        version=1,
        project_id=project_id,
        dataset_name="contacts",
        model="res.partner",
        field_names=("name",),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        max_rows=1_000,
        connection_target_hash=schema.connection_target_hash,
        schema_scope_hash=schema.content_hash,
        read_principal_hash=schema.read_principal_hash,
        read_permission_hash=schema.read_permission_hash,
        context_hash=schema.read_context_hash,
        created_at=now,
        created_by="Data Manager",
    )


if __name__ == "__main__":
    unittest.main()
