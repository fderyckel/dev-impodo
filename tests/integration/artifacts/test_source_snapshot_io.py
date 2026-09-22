from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import psutil
import re
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid4, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.adapters.duckdb.row_inclusion_review_repository import (
    RowInclusionReviewRepository,
)
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from impodo.adapters.polars_transformation import PolarsTransformationAdapter
from impodo.application.workspace.preparation.bounded_preparation import (
    direct_preparation_row_limit,
    prepare_bounded_direct_session,
)
from impodo.application.workspace.mapping.bounded_direct_review import (
    direct_row_inclusion_review,
    direct_transformation_impact,
)
from impodo.application.workspace.preparation.preparation_service import (
    stage_browser_mapping,
)
from impodo.application.source_workspace_service import SourceWorkspaceService
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarFieldMapping,
    SelectionConditionOperator,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
)
from impodo.domain.staging.transformation_impact import TransformationImpactIdentity
from impodo.application.data_version.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus, SourceFile
from impodo.domain.preparation.staging_contracts import StagingDisposition
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotPublisher,
    load_source_snapshot_table,
    open_source_snapshot_batches,
    source_snapshot_batch_rows,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy, TextTransformStep
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.workspace.access import WorkspaceAccessContext


ROOT = REPOSITORY_ROOT
HASH_B = "sha256:" + "b" * 64
COLUMNAR_TRANSFORMATIONS = PolarsTransformationAdapter()


def _lineage_id(kind: str, workspace_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"impodo-test:{kind}:{workspace_id}"))


def _data_version_id(workspace_id: str) -> str:
    return _lineage_id("data-version", workspace_id)


class _WorkspaceAccess:
    def require(self, _actor, _capability, *, workspace_id: str):
        return WorkspaceAccessContext(
            project_id=_lineage_id("project", workspace_id),
            workspace_id=workspace_id,
            data_version_id=_data_version_id(workspace_id),
            migration_run_id=_lineage_id("run", workspace_id),
        )


class SourceSnapshotIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.database = DuckDbWorkspaceDatabase(self.root)
        self.workspace_states = WorkspaceStateRepository(self.database)
        self.derived = DerivedEntityRepository(self.database)
        self.repository = SourceRepository(self.database, self.derived)
        self.artifacts = LocalArtifactStore(self.root)
        self.service = SourceWorkspaceService(
            self.workspace_states,
            self.repository,
            _WorkspaceAccess(),
            self.artifacts,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_freeze_publishes_parquet_and_preview_works_without_original(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1, Alpha ,true\nC2,,false\n"
        )
        self.repository.save_source_catalogs(
            workspace_state.workspace_id,
            (catalog,),
            actor=LOCAL_ACTOR,
        )
        self.service.confirm_source(
            workspace_state.workspace_id,
            source_file.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.service.freeze_selection(
            workspace_state.workspace_id,
            dataset_names={(source_file.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        snapshots = self.repository.get_current_source_snapshots(workspace_state.workspace_id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].row_count, 2)
        definition = _direct_mapping(selection)
        sessions = PreparationSessionRepository(self.database)

        data_version_id = _data_version_id(workspace_state.workspace_id)
        self.artifacts.delete_source(data_version_id, source_file.stored_name)
        with self.assertRaises(ArtifactStoreError):
            with self.artifacts.materialize_source(
                data_version_id,
                source_file.stored_name,
            ):
                pass

        staged = stage_browser_mapping(
            self.workspace_states.get(workspace_state.workspace_id),
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
                "impodo.application.workspace.preparation.bounded_preparation."
                "compile_browser_row_transformer",
                side_effect=AssertionError("supported snapshot used the Python oracle"),
            ),
            patch(
                "impodo.application.workspace.preparation.bounded_preparation."
                "canonical_row_from_prepared",
                side_effect=AssertionError("native path built CanonicalRow"),
            ),
        ):
            bounded = prepare_bounded_direct_session(
                self.workspace_states.get(workspace_state.workspace_id),
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
                source_snapshots=snapshots,
                columnar_batch_size=1,
            )
        self.assertEqual(len(bounded.run.rows), 2)
        self.assertIsNotNone(bounded.run.validated_content_hash)
        database_path = (
            sessions.workspace_directory(workspace_state.workspace_id) / "workspace-engine.duckdb"
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
                        workspace_state.workspace_id,
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
        staging_repository = StagingRepository(
            self.database,
            self.artifacts,
            source_selections=self.repository,
        )
        restored = staging_repository.get_canonical_staging_run(
            workspace_state.workspace_id,
            bounded.session_id,
            expected_content_hash=bounded.run.validated_content_hash,
        )
        assert restored is not None
        self.assertEqual(restored.rows, staged.canonical_run.rows)
        self.assertEqual(restored.content_hash, staged.canonical_run.content_hash)
        with patch(
            "impodo.adapters.polars_transformation."
            "PolarsTransformationAdapter.write_prepared_snapshot",
            side_effect=AssertionError("reused preparation reran Polars"),
        ):
            repeated = prepare_bounded_direct_session(
                self.workspace_states.get(workspace_state.workspace_id),
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
                source_snapshots=snapshots,
            )
        self.assertEqual(
            repeated.run.validated_content_hash,
            bounded.run.validated_content_hash,
        )

    def test_unchanged_freeze_reuses_selection_without_snapshot_publication(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name\nC1,Acme\n"
        )
        self.repository.save_source_catalogs(
            workspace_state.workspace_id,
            (catalog,),
            actor=LOCAL_ACTOR,
        )
        self.service.confirm_source(
            workspace_state.workspace_id,
            source_file.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        names = {(source_file.file_id, "csv"): "customers"}
        first = self.service.freeze_selection(
            workspace_state.workspace_id,
            dataset_names=names,
            actor=LOCAL_ACTOR,
        )
        snapshots = self.repository.get_current_source_snapshots(
            workspace_state.workspace_id
        )

        assert self.service.snapshot_publisher is not None
        with (
            patch.object(
                self.service.snapshot_publisher,
                "publish",
                side_effect=AssertionError("unchanged freeze republished a snapshot"),
            ),
            patch(
                "impodo.application.source_workspace_service.content_hash",
                side_effect=AssertionError("unchanged freeze recomputed its hash"),
            ),
        ):
            repeated = self.service.freeze_selection(
                workspace_state.workspace_id,
                dataset_names=names,
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(repeated, first)
        self.assertEqual(repeated.version, 1)
        self.assertEqual(
            self.repository.get_current_source_snapshots(workspace_state.workspace_id),
            snapshots,
        )

    def test_concurrent_unchanged_freezes_publish_one_snapshot(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name\nC1,Acme\n"
        )
        self.repository.save_source_catalogs(
            workspace_state.workspace_id,
            (catalog,),
            actor=LOCAL_ACTOR,
        )
        self.service.confirm_source(
            workspace_state.workspace_id,
            source_file.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        names = {(source_file.file_id, "csv"): "customers"}
        assert self.service.snapshot_publisher is not None
        publisher = self.service.snapshot_publisher

        with patch.object(publisher, "publish", wraps=publisher.publish) as publish:
            with ThreadPoolExecutor(max_workers=2) as executor:
                selections = tuple(
                    executor.map(
                        lambda _index: self.service.freeze_selection(
                            workspace_state.workspace_id,
                            dataset_names=names,
                            actor=LOCAL_ACTOR,
                        ),
                        range(2),
                    )
                )

        self.assertEqual(selections[0], selections[1])
        self.assertEqual(selections[0].version, 1)
        self.assertEqual(publish.call_count, 1)

    def test_writer_uses_bounded_fragments_and_round_trips_null_and_empty(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Optional\nC1,\nC2\nC3,text\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        with patch(
            "impodo.application.data_version.source_snapshots.SOURCE_SNAPSHOT_TARGET_BATCH_ROWS",
            2,
        ):
            publication = SourceSnapshotPublisher(self.artifacts).publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
        self.assertEqual(publication.input_batch_rows, 2)
        self.assertEqual(publication.fragment_count, 2)
        snapshot = publication.snapshot
        with self.artifacts.materialize_source_snapshot(
            _data_version_id(workspace_state.workspace_id),
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
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC1,Beta,false\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        sessions = PreparationSessionRepository(self.database, self.artifacts)
        bounded = prepare_bounded_direct_session(
            workspace_state,
            _direct_mapping(selection),
            1,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            None,
            sessions,
            COLUMNAR_TRANSFORMATIONS,
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
            sessions.workspace_directory(workspace_state.workspace_id) / "workspace-engine.duckdb"
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

    def test_row_inclusion_has_bounded_and_materialized_parity(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        definition = _direct_mapping(selection)
        active = selection.datasets[0].columns[2]
        mapping = replace(
            definition.datasets[0],
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        condition_id=str(uuid4()),
                        source_column_key=active.stable_key,
                        operator=SelectionConditionOperator.EQUALS,
                        comparison_value="true",
                    ),
                ),
            ),
        )
        definition = replace(definition, datasets=(mapping,))

        materialized = stage_browser_mapping(
            workspace_state,
            definition,
            selection,
            selection,
            None,
            (catalog,),
            self.artifacts,
            source_snapshots=(snapshot,),
        )
        sessions = PreparationSessionRepository(self.database, self.artifacts)
        bounded = prepare_bounded_direct_session(
            workspace_state,
            definition,
            1,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            None,
            sessions,
            COLUMNAR_TRANSFORMATIONS,
            actor=LOCAL_ACTOR,
            source_snapshots=(snapshot,),
        )

        self.assertEqual(
            tuple(bounded.run.rows),
            materialized.canonical_run.rows,
        )
        self.assertEqual(len(materialized.prepared.records), 1)
        self.assertEqual(
            [row.disposition for row in bounded.run.rows],
            [StagingDisposition.CANDIDATE, StagingDisposition.EXCLUDED],
        )
        self.assertEqual(bounded.run.reconciliation.excluded_rows, 1)
        self.assertEqual(bounded.run.datasets[0].input_rows_used, 2)

    def test_direct_stage_three_reviews_match_materialized_evidence(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1, Alpha ,true\nC2,Beta,false\nC3, Gamma ,true\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        definition = _direct_mapping(selection)
        active = selection.datasets[0].columns[2]
        mapping = replace(
            definition.datasets[0],
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        condition_id=str(uuid4()),
                        source_column_key=active.stable_key,
                        operator=SelectionConditionOperator.EQUALS,
                        comparison_value="true",
                    ),
                ),
            ),
            fields=(
                replace(
                    definition.datasets[0].fields[0],
                    transform=ScalarTransformPolicy(trim=True),
                ),
            ),
        )
        definition = replace(definition, datasets=(mapping,))
        materialized_impacts = []
        materialized = stage_browser_mapping(
            workspace_state,
            definition,
            selection,
            selection,
            None,
            (catalog,),
            self.artifacts,
            source_snapshots=(snapshot,),
            collect_transformation_impact=True,
            transformation_detail_limit=0,
            transformation_impact_sink=materialized_impacts.append,
        )
        direct_impacts = []
        direct_report = direct_transformation_impact(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            (snapshot,),
            direct_impacts.append,
        )
        direct_rows = direct_row_inclusion_review(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            (snapshot,),
        )

        self.assertEqual(direct_report, materialized.transformation_impact)
        self.assertEqual(direct_impacts, materialized_impacts)
        self.assertEqual(direct_rows, materialized.row_inclusion_review)
        self.assertEqual(direct_rows.included_count, 2)
        self.assertEqual(direct_rows.excluded_count, 1)

    def test_direct_reviews_reconcile_multiple_physical_datasets(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1, Alpha ,true\nC2,Beta,false\n"
        )
        first_selection = _selection_for(workspace_state, source_file, catalog)
        first, = first_selection.datasets
        second = replace(
            first,
            dataset_id="dataset:" + uuid4().hex[:24],
            name="second_customers",
        )
        selection = replace(
            first_selection,
            datasets=(first, second),
            content_hash="sha256:" + "2" * 64,
        )
        publisher = SourceSnapshotPublisher(self.artifacts)
        snapshots = tuple(
            publisher.publish(workspace_state, selection, dataset, catalog, source_file).snapshot
            for dataset in selection.datasets
        )
        definition = _direct_mapping(selection)
        active = first.columns[2]
        first_mapping = replace(
            definition.datasets[0],
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        condition_id=str(uuid4()),
                        source_column_key=active.stable_key,
                        operator=SelectionConditionOperator.EQUALS,
                        comparison_value="true",
                    ),
                ),
            ),
            fields=(replace(
                definition.datasets[0].fields[0],
                transform=ScalarTransformPolicy(trim=True),
            ),),
        )
        second_mapping = replace(
            first_mapping,
            dataset_id=second.dataset_id,
            row_inclusion=RowInclusionPolicy(),
        )
        definition = replace(
            definition,
            datasets=(first_mapping, second_mapping),
        )
        materialized_impacts = []
        staged = stage_browser_mapping(
            workspace_state,
            definition,
            selection,
            selection,
            None,
            (catalog,),
            self.artifacts,
            source_snapshots=snapshots,
            collect_transformation_impact=True,
            transformation_detail_limit=0,
            transformation_impact_sink=materialized_impacts.append,
        )
        direct_impacts = []
        report = direct_transformation_impact(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            snapshots,
            direct_impacts.append,
        )
        review = direct_row_inclusion_review(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            snapshots,
        )

        self.assertEqual(report, staged.transformation_impact)
        self.assertEqual(direct_impacts, materialized_impacts)
        self.assertEqual(review, staged.row_inclusion_review)
        self.assertEqual(review.source_row_count, 2)

    def test_direct_formula_effects_match_materialized_evidence(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,1000,t\nC2,2500,t\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        definition = _direct_mapping(selection)
        mapping = replace(
            definition.datasets[0],
            fields=(replace(
                definition.datasets[0].fields[0],
                target_field="amount",
                value_type="decimal",
                transform=ScalarTransformPolicy(formula="value / 1000"),
            ),),
        )
        definition = replace(definition, datasets=(mapping,))
        materialized_rows = []
        staged = stage_browser_mapping(
            workspace_state,
            definition,
            selection,
            selection,
            None,
            (catalog,),
            self.artifacts,
            source_snapshots=(snapshot,),
            collect_transformation_impact=True,
            transformation_detail_limit=0,
            transformation_impact_sink=materialized_rows.append,
        )
        direct_rows = []
        report = direct_transformation_impact(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            (snapshot,),
            direct_rows.append,
        )

        self.assertEqual(report, staged.transformation_impact)
        self.assertEqual(direct_rows, materialized_rows)
        self.assertEqual(report.changed_count, 2)

    @unittest.skipUnless(
        os.environ.get("IMPODO_RUN_BOUNDED_REVIEW_SCALE") == "1",
        "50,000-row Stage-3 review qualification is opt-in",
    )
    def test_direct_formula_impact_scan_50_000_rows(self) -> None:
        content = b"Code,Name,Active\n" + b"".join(
            f"C{index:05d},1000,t\n".encode() for index in range(50_000)
        )
        workspace_state, source_file, catalog = self._registered_csv(content)
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        definition = _direct_mapping(selection)
        mapping = replace(
            definition.datasets[0],
            fields=(replace(
                definition.datasets[0].fields[0],
                target_field="amount",
                value_type="decimal",
                transform=ScalarTransformPolicy(formula="value / 1000"),
            ),),
        )
        definition = replace(definition, datasets=(mapping,))
        impacts = 0

        def count_impact(_row) -> None:
            nonlocal impacts
            impacts += 1

        started = perf_counter()
        report = direct_transformation_impact(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            (snapshot,),
            count_impact,
        )
        elapsed = perf_counter() - started
        print(f"50,000-row formula scan: {elapsed:.2f}s", flush=True)
        self.assertEqual(report.changed_count, 50_000)
        self.assertEqual(impacts, 50_000)
        self.assertLess(elapsed, 120)

    @unittest.skipUnless(
        os.environ.get("IMPODO_RUN_BOUNDED_REVIEW_SCALE") == "1",
        "50,000-row Stage-3 review qualification is opt-in",
    )
    def test_direct_stage_three_reviews_publish_50_000_rows(self) -> None:
        content = b"Code,Name,Active\n" + b"".join(
            (
                f"C{index:05d},"
                "1000,"
                f"{'f' if index % 10 == 1 else 't'}\n"
            ).encode()
            for index in range(50_000)
        )
        workspace_state, source_file, catalog = self._registered_csv(content)
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        definition = _direct_mapping(selection)
        active = selection.datasets[0].columns[2]
        mapping = replace(
            definition.datasets[0],
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        condition_id=str(uuid4()),
                        source_column_key=active.stable_key,
                        operator=SelectionConditionOperator.EQUALS,
                        comparison_value="t",
                    ),
                ),
            ),
            fields=(
                replace(
                    definition.datasets[0].fields[0],
                    target_field="amount",
                    value_type="decimal",
                    transform=ScalarTransformPolicy(formula="value / 1000"),
                ),
            ),
        )
        definition = replace(definition, datasets=(mapping,))
        identity = TransformationImpactIdentity(
            physical_selection_hash=selection.content_hash,
            source_selection_hash=selection.content_hash,
            mapping_content_hash=definition.content_hash,
            schema_hash=definition.schema_hash,
            derived_plan_hash=None,
        )
        started = perf_counter()
        impact_snapshot = TransformationImpactRepository(
            self.database
        ).replace_transformation_impact_snapshot(
            workspace_state.workspace_id,
            identity,
            lambda sink: direct_transformation_impact(
                workspace_state,
                definition,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                (snapshot,),
                sink,
            ),
            actor=LOCAL_ACTOR,
        )
        impact_seconds = perf_counter() - started
        row_report = direct_row_inclusion_review(
            workspace_state,
            definition,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            (snapshot,),
        )
        review_scan_seconds = perf_counter() - started - impact_seconds
        row_snapshot = RowInclusionReviewRepository(
            self.database
        ).replace_current_review(
            workspace_state.workspace_id,
            row_report,
            actor=LOCAL_ACTOR,
        )
        elapsed = perf_counter() - started
        memory = psutil.Process().memory_info()
        peak_mib = getattr(memory, "peak_wset", memory.rss) / (1024 * 1024)
        print(
            f"50,000-row direct reviews: {elapsed:.2f}s "
            f"(effects {impact_seconds:.2f}s, rows scan "
            f"{review_scan_seconds:.2f}s, rows save "
            f"{elapsed - impact_seconds - review_scan_seconds:.2f}s), "
            f"process peak/observed {peak_mib:.1f} MiB",
            flush=True,
        )

        self.assertEqual(impact_snapshot.report.changed_count, 45_000)
        self.assertEqual(row_snapshot.source_row_count, 50_000)
        self.assertEqual(row_snapshot.included_count, 45_000)
        self.assertEqual(row_snapshot.excluded_count, 5_000)
        self.assertLess(elapsed, 120)
        self.assertLess(peak_mib, 900)

    def test_prepared_backed_projection_detects_artifact_corruption(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        sessions = PreparationSessionRepository(self.database, self.artifacts)
        bounded = prepare_bounded_direct_session(
            workspace_state,
            _direct_mapping(selection),
            1,
            selection,
            selection,
            (catalog,),
            self.artifacts,
            None,
            sessions,
            COLUMNAR_TRANSFORMATIONS,
            actor=LOCAL_ACTOR,
            source_snapshots=(snapshot,),
        )
        storage_key = next(
            iter(sessions.prepared_snapshot_storage_keys(workspace_state.workspace_id))
        )
        artifact_path = self.root / "ws" / workspace_state.workspace_id / storage_key
        artifact_path.write_bytes(artifact_path.read_bytes()[:16])

        with self.assertRaisesRegex(
            WorkspaceError,
            "artifact could not be verified",
        ):
            tuple(bounded.run.rows)

    def test_unsupported_mapping_uses_one_dataset_wide_python_fallback(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
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
            "impodo.adapters.polars_transformation."
            "PolarsTransformationAdapter.write_prepared_snapshot",
            side_effect=AssertionError("unsupported mapping used the native adapter"),
        ):
            bounded = prepare_bounded_direct_session(
                workspace_state,
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
                source_snapshots=(snapshot,),
            )

        self.assertEqual(
            [row.proposed_values["name"] for row in bounded.run.rows],
            ["replaced", "Beta"],
        )

    def test_supported_mapping_cannot_fall_back_without_source_snapshot(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        sessions = PreparationSessionRepository(self.database)

        with (
            patch(
                "impodo.application.workspace.preparation.bounded_preparation."
                "compile_browser_row_transformer",
                side_effect=AssertionError("supported mapping used Python"),
            ),
            self.assertRaisesRegex(ReadinessError, "source snapshot"),
        ):
            prepare_bounded_direct_session(
                workspace_state,
                _direct_mapping(selection),
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
            )

    def test_only_verified_supported_columnar_path_receives_100k_limit(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        definition = _direct_mapping(selection)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
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
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
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
                workspace_state,
                _direct_mapping(selection),
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
                source_snapshots=(snapshot,),
            )

        prepared_root = (
            self.root
            / "ws"
            / workspace_state.workspace_id
            / "snapshots"
            / "prepared"
        )
        self.assertEqual(tuple(prepared_root.rglob("*.parquet")), ())
        self.assertEqual(
            sessions.prepared_snapshot_storage_keys(workspace_state.workspace_id),
            frozenset(),
        )

    def test_cancelled_columnar_session_reuses_snapshot_on_retry(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(
            b"Code,Name,Active\nC1,Alpha,true\nC2,Beta,false\n"
        )
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
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
                workspace_state,
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
                source_snapshots=(snapshot,),
                batch_progress=cancel_after_durable_batch,
                columnar_batch_size=1,
            )

        self.assertEqual(
            len(sessions.prepared_snapshot_storage_keys(workspace_state.workspace_id)),
            1,
        )
        with patch(
            "impodo.adapters.polars_transformation."
            "PolarsTransformationAdapter.write_prepared_snapshot",
            side_effect=AssertionError("retry reran Polars"),
        ):
            retry = prepare_bounded_direct_session(
                workspace_state,
                definition,
                1,
                selection,
                selection,
                (catalog,),
                self.artifacts,
                None,
                sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=LOCAL_ACTOR,
                source_snapshots=(snapshot,),
                columnar_batch_size=1,
            )
        self.assertEqual(len(retry.run.rows), 2)

    def test_mixed_xlsx_scalars_round_trip_through_parquet(self) -> None:
        workspace_state, source_file, catalog, selection = self._registered_xlsx()
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        with self.artifacts.materialize_source_snapshot(
            _data_version_id(workspace_state.workspace_id),
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

    def test_xlsx_without_declared_dimensions_round_trips_through_parquet(self) -> None:
        workspace_state, source_file, catalog, selection = self._registered_xlsx(
            declared_dimensions=False,
        )

        snapshot = SourceSnapshotPublisher(self.artifacts).publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot

        self.assertEqual(snapshot.row_count, 1)
        self.assertEqual(len(snapshot.schema.columns), 6)

    def test_snapshot_hash_mismatch_and_truncation_fail_closed(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        path = (
            self.root
            / "dv"
            / _data_version_id(workspace_state.workspace_id)
            / snapshot.parquet_storage_key
        )
        path.write_bytes(path.read_bytes()[:16])
        with self.assertRaisesRegex(ArtifactStoreError, "hash verification"):
            with self.artifacts.materialize_source_snapshot(
                _data_version_id(workspace_state.workspace_id),
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ):
                pass

    def test_identical_ingestion_reuses_the_content_addressed_file(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(workspace_state, source_file, catalog)
        publisher = SourceSnapshotPublisher(self.artifacts)
        first = publisher.publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        second = publisher.publish(
            workspace_state,
            selection,
            selection.datasets[0],
            catalog,
            source_file,
        ).snapshot
        self.assertEqual(second.logical_hash, first.logical_hash)
        self.assertEqual(second.parquet_sha256, first.parquet_sha256)
        self.assertEqual(second.parquet_storage_key, first.parquet_storage_key)

    def test_write_failure_leaves_no_partial_or_published_snapshot(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(workspace_state, source_file, catalog)
        with (
            patch(
                "polars.DataFrame.write_parquet",
                side_effect=OSError("disk full"),
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            SourceSnapshotPublisher(self.artifacts).publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
        snapshot_root = (
            self.root
            / "dv"
            / _data_version_id(workspace_state.workspace_id)
            / "snapshots"
            / "source"
        )
        self.assertEqual(tuple(snapshot_root.rglob("*.parquet")), ())
        work = snapshot_root / ".work"
        self.assertEqual(tuple(work.iterdir()), ())

    def test_failed_pointer_transaction_preserves_previous_selection(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        first = _selection_for(workspace_state, source_file, catalog)
        first_snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                first,
                first.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        self.repository.publish_source_selection_with_snapshots(
            workspace_state.workspace_id,
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
            data_version_id=_data_version_id(workspace_state.workspace_id),
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
                workspace_state.workspace_id,
                second,
                (second_snapshot,),
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(
            self.repository.get_source_selection(workspace_state.workspace_id),
            first,
        )
        self.assertEqual(
            self.repository.get_current_source_snapshots(workspace_state.workspace_id),
            (first_snapshot,),
        )

    def test_cleanup_removes_only_unregistered_snapshot_files(self) -> None:
        workspace_state, source_file, catalog = self._registered_csv(b"Code\nC1\n")
        selection = _selection_for(workspace_state, source_file, catalog)
        snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                workspace_state,
                selection,
                selection.datasets[0],
                catalog,
                source_file,
            )
            .snapshot
        )
        data_version_id = _data_version_id(workspace_state.workspace_id)
        snapshot_path = (
            self.root
            / "dv"
            / data_version_id
            / snapshot.parquet_storage_key
        )
        self.assertEqual(
            self.artifacts.cleanup_source_snapshots(
                data_version_id,
                frozenset((snapshot.parquet_storage_key,)),
            ),
            0,
        )
        self.assertTrue(snapshot_path.is_file())
        removed = self.artifacts.cleanup_source_snapshots(
            data_version_id,
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
        workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Snapshot ingestion",
            source_system="CSV",
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspace_states.initialize_workbench(workspace_state, actor=LOCAL_ACTOR)
        stored = self.artifacts.store_source(
            _data_version_id(workspace_state.workspace_id),
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
        workspace_state = replace(
            workspace_state,
            source_files=(source_file,),
            revision=2,
            updated_at=now,
        )
        self.workspace_states.add_source_file(
            workspace_state,
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
        return self.workspace_states.get(workspace_state.workspace_id), source_file, catalog

    def _registered_xlsx(
        self,
        *,
        declared_dimensions: bool = True,
    ):
        from openpyxl import Workbook
        from impodo.application.source_workspace_service import (
            _column_key,
            _dataset_key,
        )
        from impodo.domain.workspace.contracts import (
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
        source_bytes = content.getvalue()
        if not declared_dimensions:
            rewritten = BytesIO()
            with ZipFile(BytesIO(source_bytes)) as reader, ZipFile(
                rewritten,
                mode="w",
                compression=ZIP_DEFLATED,
            ) as writer:
                for member in reader.infolist():
                    payload = reader.read(member.filename)
                    if member.filename == "xl/worksheets/sheet1.xml":
                        payload, count = re.subn(
                            rb"<dimension[^>]*/>",
                            b"",
                            payload,
                            count=1,
                        )
                        if count != 1:
                            raise AssertionError("XLSX fixture has no dimension")
                    writer.writestr(member, payload)
            source_bytes = rewritten.getvalue()
        now = datetime.now(timezone.utc)
        workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="XLSX snapshot ingestion",
            source_system="XLSX",
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspace_states.initialize_workbench(workspace_state, actor=LOCAL_ACTOR)
        stored = self.artifacts.store_source(
            _data_version_id(workspace_state.workspace_id),
            artifact_id=str(uuid4()),
            suffix=".xlsx",
            stream=BytesIO(source_bytes),
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
        workspace_state = replace(
            workspace_state,
            source_files=(source_file,),
            revision=2,
            updated_at=now,
        )
        self.workspace_states.add_source_file(
            workspace_state,
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
            data_version_id=_data_version_id(workspace_state.workspace_id),
            created_at=now,
            created_by="Tester",
            datasets=(dataset,),
            content_hash="sha256:" + "3" * 64,
        )
        return self.workspace_states.get(workspace_state.workspace_id), source_file, catalog, selection


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
    workspace_state: WorkspaceState,
    source_file: SourceFile,
    catalog: SourceFileCatalog,
):
    from impodo.application.source_workspace_service import _column_key, _dataset_key
    from impodo.domain.workspace.contracts import (
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
        data_version_id=_data_version_id(workspace_state.workspace_id),
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
