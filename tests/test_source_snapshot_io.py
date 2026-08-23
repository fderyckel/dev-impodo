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
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.application.bounded_preparation import (
    direct_preparation_row_limit,
    prepare_bounded_direct_session,
)
from impodo.application.preparation_service import stage_browser_mapping
from impodo.application.source_workspace_service import SourceWorkspaceService
from impodo.artifacts import ArtifactStoreError, LocalArtifactStore
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
)
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.models import canonical_json_bytes
from impodo.workspace_state import WorkspaceState, WorkspaceStatus, SourceFile
from impodo.staging_contracts import StagingDisposition
from impodo.source_snapshot_io import (
    SourceSnapshotPublisher,
    load_source_snapshot_table,
    open_source_snapshot_batches,
    source_snapshot_batch_rows,
)
from impodo.value_rules import ScalarTransformPolicy, TextTransformStep
from impodo.workspace_errors import WorkspaceError


ROOT = Path(__file__).resolve().parents[1]
HASH_B = "sha256:" + "b" * 64


class SourceSnapshotIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.database = DuckDbWorkspaceDatabase(self.root)
        self.projects = WorkspaceStateRepository(self.database)
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
        with (
            patch(
                "impodo.application.bounded_preparation.compile_browser_row_transformer",
                side_effect=AssertionError("supported snapshot used the Python oracle"),
            ),
            patch(
                "impodo.application.bounded_preparation.canonical_row_from_prepared",
                side_effect=AssertionError("native path built CanonicalRow"),
            ),
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
        database_path = (
            sessions.workspace_directory(project.project_id) / "workspace-engine.duckdb"
        )
        with sessions._connect(database_path) as connection:
            storage = connection.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(LENGTH(row_json)), 0)
                       FROM canonical_staging_row
                      WHERE run_id = ?),
                    (SELECT COUNT(*)
                       FROM canonical_prepared_projection
                      WHERE run_id = ?)
                """,
                [bounded.session_id, bounded.session_id],
            ).fetchone()
        self.assertEqual(storage, (0, 1))
        expected_encoded_rows = tuple(
            canonical_json_bytes(row.to_portable_dict()).decode("utf-8")
            for row in staged.canonical_run.rows
        )
        for batch_size in (1, 17, 5_000):
            with self.subTest(projection_batch_size=batch_size):
                encoded_rows = tuple(
                    str(item[-1])
                    for batch in sessions._iter_direct_encoded_batches(
                        project.project_id,
                        bounded.session_id,
                        batch_size=batch_size,
                    )
                    for item in batch
                )
                self.assertEqual(encoded_rows, expected_encoded_rows)
        self.assertEqual(
            tuple(bounded.run.rows),
            tuple(staged.canonical_run.rows),
        )
        self.assertEqual(
            bounded.run.validated_content_hash,
            staged.canonical_run.content_hash,
        )
        with sessions._connect(database_path) as connection:
            connection.execute(
                "DELETE FROM preparation_session_snapshot WHERE session_id = ?",
                [bounded.session_id],
            )
        staging_repository = StagingRepository(self.database, self.artifacts)
        restored = staging_repository.get_canonical_staging_run(
            project.project_id,
            bounded.session_id,
            expected_content_hash=bounded.run.validated_content_hash,
        )
        assert restored is not None
        self.assertEqual(restored.rows, staged.canonical_run.rows)
        self.assertEqual(restored.content_hash, staged.canonical_run.content_hash)
        with patch(
            "impodo.application.bounded_preparation.write_polars_prepared_snapshot",
            side_effect=AssertionError("reused preparation reran Polars"),
        ):
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

    def test_prepared_backed_duplicate_issues_are_sparse_overlays(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC1,Beta,false\n"
        )
        selection = _selection_for(project, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        sessions = PreparationSessionRepository(self.database, self.artifacts)
        bounded = prepare_bounded_direct_session(
            project,
            _direct_mapping(selection),
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

        rows = tuple(bounded.run.rows)
        self.assertTrue(
            all(row.disposition is StagingDisposition.BLOCKED for row in rows)
        )
        self.assertTrue(
            all(
                any(issue.code == "SOURCE_IDENTITY_DUPLICATE" for issue in row.issues)
                for row in rows
            )
        )
        database_path = (
            sessions.workspace_directory(project.project_id) / "workspace-engine.duckdb"
        )
        with sessions._connect(database_path) as connection:
            storage = connection.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(LENGTH(row_json)), 0)
                       FROM canonical_staging_row WHERE run_id = ?),
                    (SELECT COUNT(*) FROM canonical_staging_row_issue
                      WHERE run_id = ?)
                """,
                [bounded.session_id, bounded.session_id],
            ).fetchone()
        self.assertEqual(storage, (0, 2))

    def test_prepared_backed_projection_detects_artifact_corruption(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(project, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        sessions = PreparationSessionRepository(self.database, self.artifacts)
        bounded = prepare_bounded_direct_session(
            project,
            _direct_mapping(selection),
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
        storage_key = next(
            iter(sessions.prepared_snapshot_storage_keys(project.project_id))
        )
        artifact_path = self.root / project.project_id / storage_key
        artifact_path.write_bytes(artifact_path.read_bytes()[:16])

        with self.assertRaisesRegex(
            WorkspaceError,
            "artifact could not be verified",
        ):
            tuple(bounded.run.rows)

    def test_unsupported_mapping_uses_one_dataset_wide_python_fallback(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(project, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
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
                                text_steps=(
                                    TextTransformStep(
                                        search_value="^A.*$",
                                        replacement_value="replaced",
                                        search_mode="pattern",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        sessions = PreparationSessionRepository(self.database)

        with patch(
            "impodo.application.bounded_preparation.write_polars_prepared_snapshot",
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

    def test_supported_mapping_cannot_fall_back_without_source_snapshot(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\n"
        )
        selection = _selection_for(project, source_file, catalog)
        sessions = PreparationSessionRepository(self.database)

        with (
            patch(
                "impodo.application.bounded_preparation.compile_browser_row_transformer",
                side_effect=AssertionError("supported mapping used Python"),
            ),
            self.assertRaisesRegex(ReadinessError, "source snapshot"),
        ):
            prepare_bounded_direct_session(
                project,
                _direct_mapping(selection),
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                actor=LOCAL_ACTOR,
            )

    def test_only_verified_supported_columnar_path_receives_100k_limit(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\n"
        )
        selection = _selection_for(project, source_file, catalog)
        definition = _direct_mapping(selection)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )

        self.assertEqual(
            direct_preparation_row_limit(
                definition,
                selection,
                (snapshot,),
            ),
            COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )
        self.assertEqual(
            direct_preparation_row_limit(definition, selection, ()),
            BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )

        dataset_mapping = definition.datasets[0]
        unsupported = replace(
            definition,
            datasets=(
                replace(
                    dataset_mapping,
                    fields=(
                        replace(
                            dataset_mapping.fields[0],
                            transform=ScalarTransformPolicy(
                                text_steps=(
                                    TextTransformStep(
                                        search_value="^A.*$",
                                        replacement_value="replaced",
                                        search_mode="pattern",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(
            direct_preparation_row_limit(
                unsupported,
                selection,
                (snapshot,),
            ),
            BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )

    def test_prepared_snapshot_bind_failure_removes_unregistered_file(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(project, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        sessions = PreparationSessionRepository(self.database)

        with (
            patch.object(
                sessions,
                "bind_prepared_snapshot",
                side_effect=RuntimeError("injected manifest failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected manifest failure"),
        ):
            prepare_bounded_direct_session(
                project,
                _direct_mapping(selection),
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

        prepared_root = self.root / project.project_id / "snapshots" / "prepared"
        self.assertEqual(tuple(prepared_root.rglob("*.parquet")), ())
        self.assertEqual(
            sessions.prepared_snapshot_storage_keys(project.project_id),
            frozenset(),
        )

    def test_cancelled_columnar_session_reuses_snapshot_on_retry(self) -> None:
        project, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(project, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        sessions = PreparationSessionRepository(self.database)
        definition = _direct_mapping(selection)

        def cancel_after_durable_batch(_completed: int, _total: int) -> None:
            raise RuntimeError("injected cancellation")

        with self.assertRaisesRegex(RuntimeError, "injected cancellation"):
            prepare_bounded_direct_session(
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
                batch_progress=cancel_after_durable_batch,
                columnar_batch_size=1,
            )

        self.assertEqual(
            len(sessions.prepared_snapshot_storage_keys(project.project_id)),
            1,
        )
        with patch(
            "impodo.application.bounded_preparation.write_polars_prepared_snapshot",
            side_effect=AssertionError("retry reran Polars"),
        ):
            retry = prepare_bounded_direct_session(
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
                columnar_batch_size=1,
            )
        self.assertEqual(len(retry.run.rows), 2)

    def test_mixed_xlsx_scalars_round_trip_through_parquet(self) -> None:
        project, source_file, catalog, selection = self._registered_xlsx()
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        with self.artifacts.materialize_source_snapshot(
            project.project_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            table = load_source_snapshot_table(path, snapshot)
        row = table.rows[0]
        self.assertEqual(row.values["Text"], "Ã…ngstrÃ¶m æ±äº¬")
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
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
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
        first_snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                first,
                first.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
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
            source=second.datasets[0].source,
            physical_selection_hash=second.content_hash,
            schema=first_snapshot.schema,
            row_count=1,
            data_logical_hash=first_snapshot.data_logical_hash,
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
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                project,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
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
    ) -> tuple[WorkspaceState, SourceFile, SourceFileCatalog]:
        now = datetime.now(timezone.utc)
        project = WorkspaceState(
            project_id=str(uuid4()),
            name="Snapshot ingestion",
            source_system="CSV",
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.projects.create_unlinked(project, actor=LOCAL_ACTOR)
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
        columns = tuple(
            _column(index, name, row_count) for index, name in enumerate(headers, 1)
        )
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
                "Ã…ngstrÃ¶m æ±äº¬",
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
        project = WorkspaceState(
            project_id=str(uuid4()),
            name="XLSX snapshot ingestion",
            source_system="XLSX",
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.projects.create_unlinked(project, actor=LOCAL_ACTOR)
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
            source=FileSourceBinding(
                file_id=source_file.file_id,
                table_key=table.table_key,
                source_sha256=source_file.sha256,
                catalog_hash=catalog.content_hash,
                encoding=None,
                delimiter=None,
                header_row=1,
            ),
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
    project: WorkspaceState,
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
        source=FileSourceBinding(
            file_id=source_file.file_id,
            table_key="csv",
            source_sha256=source_file.sha256,
            catalog_hash=catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
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

