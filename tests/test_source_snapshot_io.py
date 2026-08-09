from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.application.bounded_preparation import prepare_bounded_direct_session
from impodo.application.preparation_service import stage_browser_mapping
from impodo.application.source_workspace_service import SourceWorkspaceService
from impodo.artifacts import ArtifactStoreError, LocalArtifactStore
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.projects import MigrationProject, ProjectStatus, SourceFile
from impodo.source import SourceLoadError
from impodo.source_snapshot_io import (
    SourceSnapshotPublisher,
    load_source_snapshot_table,
    open_source_snapshot_batches,
    source_snapshot_batch_rows,
)
from impodo.value_rules import ScalarTransformPolicy


ROOT = Path(__file__).resolve().parents[1]
HASH_B = "sha256:" + "b" * 64


class SourceSnapshotIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.database = DuckDbDatabase(self.root)
        self.projects = ProjectRepository(self.database)
        self.derived = DerivedEntityRepository(self.database)
        self.repository = SourceRepository(self.database, self.derived)
        self.artifacts = LocalArtifactStore(self.root)
        self.service = SourceWorkspaceService(
            self.projects,
            self.repository,
            CapabilityAuthorizationPolicy(),
            self.artifacts,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_freeze_publishes_parquet_and_preview_works_without_original(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1, Alpha ,true\nC2,,false\n"
        )
        self.repository.save_source_catalogs(
            project.project_id,
            (catalog,),
            actor=LOCAL_ACTOR,
        )
        self.service.confirm_source(
            project.project_id,
            source_file.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.service.freeze_selection(
            project.project_id,
            dataset_names={(source_file.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        snapshots = self.repository.get_current_source_snapshots(project.project_id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].row_count, 2)
        definition = _direct_mapping(selection)
        sessions = PreparationSessionRepository(self.database)
        python_bounded = prepare_bounded_direct_session(
            self.projects.get(project.project_id),
            definition,
            1,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            None,
            sessions,
            actor=LOCAL_ACTOR,
        )

        self.artifacts.delete_source(project.project_id, source_file.stored_name)
        with self.assertRaises(ArtifactStoreError):
            with self.artifacts.materialize_source(
                project.project_id,
                source_file.stored_name,
            ):
                pass

        staged = stage_browser_mapping(
            self.projects.get(project.project_id),
            definition,
            selection,
            selection,
            None,
            (catalog,),
            self.artifacts,
            source_snapshots=snapshots,
        )
        self.assertEqual(
            [record.source_identity for record in staged.prepared.records],
            [("C1",), ("C2",)],
        )
        self.assertEqual(
            [record.scalar_values["name"] for record in staged.prepared.records],
            [" Alpha ", None],
        )
        with patch(
            "impodo.application.bounded_preparation.compile_browser_row_transformer",
            side_effect=AssertionError("supported snapshot used the Python oracle"),
        ):
            bounded = prepare_bounded_direct_session(
                self.projects.get(project.project_id),
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                actor=LOCAL_ACTOR,
                source_snapshots=snapshots,
                columnar_batch_size=1,
            )
        self.assertEqual(len(bounded.run.rows), 2)
        self.assertIsNotNone(bounded.run.validated_content_hash)
        self.assertEqual(tuple(bounded.run.rows), tuple(python_bounded.run.rows))
        self.assertEqual(
            tuple(sessions.iter_impacts(project.project_id, bounded.session_id)),
            tuple(
                sessions.iter_impacts(
                    project.project_id,
                    python_bounded.session_id,
                )
            ),
        )
        self.assertEqual(
            bounded.run.validated_content_hash,
            python_bounded.run.validated_content_hash,
        )
        repeated = prepare_bounded_direct_session(
            self.projects.get(project.project_id),
            definition,
            1,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            None,
            sessions,
            actor=LOCAL_ACTOR,
            source_snapshots=snapshots,
        )
        self.assertEqual(
            repeated.run.validated_content_hash,
            bounded.run.validated_content_hash,
        )

    def test_writer_uses_bounded_fragments_and_round_trips_null_and_empty(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Optional\nC1,\nC2\nC3,text\n"
        )
        selection = _selection_for(project, source_file, catalog)
        with patch(
            "impodo.source_snapshot_io.SOURCE_SNAPSHOT_TARGET_BATCH_ROWS",
            2,
        ):
            publication = SourceSnapshotPublisher(self.artifacts).publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
        self.assertEqual(publication.input_batch_rows, 2)
        self.assertEqual(publication.fragment_count, 2)
        snapshot = publication.snapshot
        with self.artifacts.materialize_source_snapshot(
            project.project_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            with open_source_snapshot_batches(path, snapshot, batch_size=1) as stream:
                rows = [row for batch in stream.iter_batches() for row in batch]
        self.assertEqual(
            [row.values["Optional"] for row in rows],
            ["", None, "text"],
        )
        self.assertEqual([row.number for row in rows], [2, 3, 4])

    def test_unsupported_mapping_uses_one_dataset_wide_python_fallback(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(project, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            project,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        definition = _direct_mapping(selection)
        dataset_mapping = definition.datasets[0]
        name_field = dataset_mapping.fields[0]
        definition = replace(
            definition,
            datasets=(
                replace(
                    dataset_mapping,
                    fields=(
                        replace(
                            name_field,
                            transform=ScalarTransformPolicy(
                                search_value="^A.*$",
                                replacement_value="replaced",
                                search_mode="pattern",
                            ),
                        ),
                    ),
                ),
            ),
        )
        sessions = PreparationSessionRepository(self.database)

        with patch(
            "impodo.application.bounded_preparation.iter_polars_transformation_batches",
            side_effect=AssertionError("unsupported mapping used the native adapter"),
        ):
            bounded = prepare_bounded_direct_session(
                project,
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                actor=LOCAL_ACTOR,
                source_snapshots=(snapshot,),
            )

        self.assertEqual(
            [row.proposed_values["name"] for row in bounded.run.rows],
            ["replaced", "Beta"],
        )

    def test_mixed_xlsx_scalars_round_trip_through_parquet(self) -> None:
        project, source_file, catalog, selection = self._registered_xlsx()
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            project,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        with self.artifacts.materialize_source_snapshot(
            project.project_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            table = load_source_snapshot_table(path, snapshot)
        row = table.rows[0]
        self.assertEqual(row.values["Text"], "Ångström 東京")
        self.assertIs(row.values["Boolean"], True)
        self.assertEqual(row.values["Integer"], 9_007_199_254_740_991)
        self.assertEqual(row.values["Float"], 12.5)
        self.assertEqual(row.values["Date"], datetime(2026, 8, 9, 0, 0))
        self.assertEqual(
            row.values["DateTime"],
            datetime(2026, 8, 9, 14, 30, 15),
        )

    def test_snapshot_hash_mismatch_and_truncation_fail_closed(self) -> None:
        project, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(project, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            project,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        path = self.root / project.project_id / snapshot.parquet_storage_key
        path.write_bytes(path.read_bytes()[:16])
        with self.assertRaisesRegex(ArtifactStoreError, "hash verification"):
            with self.artifacts.materialize_source_snapshot(
                project.project_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ):
                pass

    def test_identical_ingestion_reuses_the_content_addressed_file(self) -> None:
        project, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(project, source_file, catalog)
        publisher = SourceSnapshotPublisher(self.artifacts)
        first = publisher.publish(
            project,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        second = publisher.publish(
            project,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        self.assertEqual(second.logical_hash, first.logical_hash)
        self.assertEqual(second.parquet_sha256, first.parquet_sha256)
        self.assertEqual(second.parquet_storage_key, first.parquet_storage_key)

    def test_write_failure_leaves_no_partial_or_published_snapshot(self) -> None:
        project, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(project, source_file, catalog)
        with (
            patch(
                "polars.DataFrame.write_parquet",
                side_effect=OSError("disk full"),
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            SourceSnapshotPublisher(self.artifacts).publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
        snapshot_root = self.root / project.project_id / "snapshots" / "source"
        self.assertEqual(tuple(snapshot_root.rglob("*.parquet")), ())
        work = snapshot_root / ".work"
        self.assertEqual(tuple(work.iterdir()), ())

    def test_failed_pointer_transaction_preserves_previous_selection(self) -> None:
        project, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        first = _selection_for(project, source_file, catalog)
        first_snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            project,
            first,
            first.datasets[0],
            catalog,
            source_file,
        ).snapshot
        self.repository.publish_source_selection_with_snapshots(
            project.project_id,
            first,
            (first_snapshot,),
            actor=LOCAL_ACTOR,
        )
        second = replace(
            first,
            selection_id=str(uuid4()),
            version=2,
            content_hash="sha256:" + "2" * 64,
        )
        second_snapshot = SourceSnapshot.create(
            project_id=project.project_id,
            dataset_id=second.datasets[0].dataset_id,
            dataset_name=second.datasets[0].name,
            file_id=source_file.file_id,
            table_key="csv",
            source_sha256="sha256:" + source_file.sha256,
            catalog_hash=catalog.content_hash,
            physical_selection_hash=second.content_hash,
            schema=first_snapshot.schema,
            row_count=1,
            parquet_sha256=first_snapshot.parquet_sha256,
            created_at=datetime.now(timezone.utc),
        )
        with (
            patch.object(
                self.database,
                "_insert_workspace_audit",
                side_effect=RuntimeError("injected audit failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected audit failure"),
        ):
            self.repository.publish_source_selection_with_snapshots(
                project.project_id,
                second,
                (second_snapshot,),
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(
            self.repository.get_source_selection(project.project_id),
            first,
        )
        self.assertEqual(
            self.repository.get_current_source_snapshots(project.project_id),
            (first_snapshot,),
        )

    def test_cleanup_removes_only_unregistered_snapshot_files(self) -> None:
        project, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(project, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            project,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        snapshot_path = self.root / project.project_id / snapshot.parquet_storage_key
        self.assertEqual(
            self.artifacts.cleanup_source_snapshots(
                project.project_id,
                frozenset((snapshot.parquet_storage_key,)),
            ),
            0,
        )
        self.assertTrue(snapshot_path.is_file())
        removed = self.artifacts.cleanup_source_snapshots(
            project.project_id,
            frozenset(),
        )
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(snapshot_path.exists())

    def test_wide_source_batch_cap_is_cell_bounded(self) -> None:
        self.assertEqual(source_snapshot_batch_rows(1), 5_000)
        self.assertEqual(source_snapshot_batch_rows(2_048), 48)

    def _registered_csv(
        self,
        content: bytes,
    ) -> tuple[MigrationProject, SourceFile, SourceFileCatalog]:
        now = datetime.now(timezone.utc)
        project = MigrationProject(
            project_id=str(uuid4()),
            name="Snapshot ingestion",
            source_system="CSV",
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        self.projects.create(project, actor=LOCAL_ACTOR)
        stored = self.artifacts.store_source(
            project.project_id,
            artifact_id=str(uuid4()),
            suffix=".csv",
            stream=BytesIO(content),
            maximum_bytes=1024 * 1024,
            chunk_bytes=17,
            validator=lambda _path: None,
        )
        source_file = SourceFile(
            file_id=str(uuid4()),
            display_name="customers.csv",
            stored_name=stored.storage_key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256.removeprefix("sha256:"),
            received_at=now,
        )
        project = replace(
            project,
            source_files=(source_file,),
            revision=2,
            updated_at=now,
        )
        self.projects.add_source_file(
            project,
            source_file,
            expected_revision=1,
            actor=LOCAL_ACTOR,
        )
        headers = content.splitlines()[0].decode("utf-8").split(",")
        row_count = len(content.splitlines()) - 1
        columns = tuple(_column(index, name, row_count) for index, name in enumerate(headers, 1))
        table = SourceTableCatalog(
            table_key="csv",
            name="customers",
            kind="CSV",
            hidden=False,
            header_row=1,
            row_count=row_count,
            column_count=len(columns),
            columns=columns,
            preview_rows=(),
        )
        catalog = SourceFileCatalog(
            contract_version=2,
            file_id=source_file.file_id,
            display_name=source_file.display_name,
            source_sha256=source_file.sha256,
            source_size_bytes=source_file.size_bytes,
            format="csv",
            inspected_at=now,
            encoding="utf-8",
            delimiter=",",
            tables=(table,),
        )
        return self.projects.get(project.project_id), source_file, catalog

    def _registered_xlsx(
        self,
    ):
        from openpyxl import Workbook
        from impodo.application.source_workspace_service import (
            _column_key,
            _dataset_key,
        )
        from impodo.workspace_contracts import (
            SourceDataset,
            SourceDatasetColumn,
            SourceSelection,
        )

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Data"
        headers = ("Text", "Boolean", "Integer", "Float", "Date", "DateTime")
        worksheet.append(headers)
        worksheet.append(
            (
                "Ångström 東京",
                True,
                9_007_199_254_740_991,
                12.5,
                date(2026, 8, 9),
                datetime(2026, 8, 9, 14, 30, 15),
            )
        )
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        now = datetime.now(timezone.utc)
        project = MigrationProject(
            project_id=str(uuid4()),
            name="XLSX snapshot ingestion",
            source_system="XLSX",
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        self.projects.create(project, actor=LOCAL_ACTOR)
        stored = self.artifacts.store_source(
            project.project_id,
            artifact_id=str(uuid4()),
            suffix=".xlsx",
            stream=BytesIO(content.getvalue()),
            maximum_bytes=1024 * 1024,
            chunk_bytes=101,
            validator=lambda _path: None,
        )
        source_file = SourceFile(
            file_id=str(uuid4()),
            display_name="mixed.xlsx",
            stored_name=stored.storage_key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256.removeprefix("sha256:"),
            received_at=now,
        )
        project = replace(
            project,
            source_files=(source_file,),
            revision=2,
            updated_at=now,
        )
        self.projects.add_source_file(
            project,
            source_file,
            expected_revision=1,
            actor=LOCAL_ACTOR,
        )
        profiles = tuple(
            _column(index, name, 1) for index, name in enumerate(headers, 1)
        )
        table = SourceTableCatalog(
            table_key="sheet:Data",
            name="Data",
            kind="WORKSHEET",
            hidden=False,
            header_row=1,
            row_count=1,
            column_count=len(headers),
            columns=profiles,
            preview_rows=(),
        )
        catalog = SourceFileCatalog(
            contract_version=2,
            file_id=source_file.file_id,
            display_name=source_file.display_name,
            source_sha256=source_file.sha256,
            source_size_bytes=source_file.size_bytes,
            format="xlsx",
            inspected_at=now,
            encoding=None,
            delimiter=None,
            tables=(table,),
        )
        dataset = SourceDataset(
            dataset_id=_dataset_key(source_file.file_id, table.table_key),
            name="mixed",
            file_id=source_file.file_id,
            table_key=table.table_key,
            source_sha256=source_file.sha256,
            catalog_hash=catalog.content_hash,
            encoding=None,
            delimiter=None,
            header_row=1,
            row_count=1,
            columns=tuple(
                SourceDatasetColumn(
                    ordinal=item.ordinal,
                    source_name=item.name,
                    stable_key=_column_key(item.ordinal, item.name),
                    candidate_type=item.candidate_type,
                )
                for item in profiles
            ),
        )
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=project.project_id,
            created_at=now,
            created_by="Tester",
            datasets=(dataset,),
            content_hash="sha256:" + "3" * 64,
        )
        return self.projects.get(project.project_id), source_file, catalog, selection


def _column(ordinal: int, name: str, row_count: int) -> SourceColumnProfile:
    return SourceColumnProfile(
        ordinal=ordinal,
        name=name,
        candidate_type="string",
        null_count=0,
        non_null_count=row_count,
        distinct_count=row_count,
        distinct_count_is_exact=True,
        duplicate_count=0,
        minimum=None,
        maximum=None,
        minimum_length=None,
        maximum_length=None,
    )


def _selection_for(
    project: MigrationProject,
    source_file: SourceFile,
    catalog: SourceFileCatalog,
):
    from impodo.application.source_workspace_service import _column_key, _dataset_key
    from impodo.workspace_contracts import (
        SourceDataset,
        SourceDatasetColumn,
        SourceSelection,
    )

    table = catalog.tables[0]
    dataset = SourceDataset(
        dataset_id=_dataset_key(source_file.file_id, "csv"),
        name="customers",
        file_id=source_file.file_id,
        table_key="csv",
        source_sha256=source_file.sha256,
        catalog_hash=catalog.content_hash,
        encoding="utf-8",
        delimiter=",",
        header_row=1,
        row_count=table.row_count,
        columns=tuple(
            SourceDatasetColumn(
                ordinal=item.ordinal,
                source_name=item.name,
                stable_key=_column_key(item.ordinal, item.name),
                candidate_type=item.candidate_type,
            )
            for item in table.columns
        ),
    )
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        project_id=project.project_id,
        created_at=datetime.now(timezone.utc),
        created_by="Tester",
        datasets=(dataset,),
        content_hash="sha256:" + "1" * 64,
    )


def _direct_mapping(selection) -> MappingDefinition:
    dataset = selection.datasets[0]
    code, name, _active = dataset.columns
    return MappingDefinition(
        mapping_id=str(uuid4()),
        source_selection_hash=selection.content_hash,
        schema_hash=HASH_B,
        datasets=(
            DatasetMapping(
                dataset_id=dataset.dataset_id,
                target_model="res.partner",
                source_identity_column_keys=(code.stable_key,),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=(code.stable_key,),
                        target_fields=("ref",),
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        target_field="name",
                        source_column_key=name.stable_key,
                        value_type="string",
                        required=False,
                    ),
                ),
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
