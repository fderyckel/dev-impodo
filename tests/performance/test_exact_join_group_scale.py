from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

import polars as pl

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.exact_join_group_evaluator import (
    DuckDbExactJoinGroupEvaluator,
)
from impodo.adapters.duckdb.grouped_exact_join_source import (
    DuckDbGroupedExactJoinSource,
)
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.derived_entities import (
    DerivedEntityWorkspaceService,
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    BoundedExactJoinGroup,
    prepare_bounded_grouped_exact_join_session,
)
from impodo.application.workspace.preparation.preparation_capability import (
    compile_preparation_capability,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import SourceCellKind, SourceSnapshot
from impodo.domain.structural import structural_dataset_id, structural_mapping_selection
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    ExactJoinInputSnapshotEvidence,
    SavedExactJoinReview,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    mapping_source_selection,
)
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus

_ROW_COUNTS = (21_766, 21_766, 21_767)
_FIELD_COUNTS = (88, 87, 87)
_EMPTY_FIELD_COUNTS = (33, 32, 32)


class ExactJoinGroupScaleTests(unittest.TestCase):
    def test_workspace_shaped_review_scans_each_wide_snapshot_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection()
            database = DuckDbWorkspaceDatabase(root / "workspace-databases")
            artifacts = LocalArtifactStore(root / "artifacts")
            snapshots: dict[str, SourceSnapshot] = {}
            paths: dict[str, Path] = {}
            for dataset in selection.datasets:
                path = root / f"{dataset.name}.parquet"
                _write_wide_snapshot(path, dataset)
                schema = source_snapshot_schema(dataset)
                snapshot = SourceSnapshot.create(
                    data_version_id=selection.data_version_id,
                    dataset_id=dataset.dataset_id,
                    dataset_name=dataset.name,
                    source=dataset.source,
                    physical_selection_hash=selection.content_hash,
                    schema=schema,
                    row_count=dataset.row_count,
                    data_logical_hash="sha256:" + "4" * 64,
                    parquet_sha256=_file_sha256(path),
                    created_at=selection.created_at,
                )
                snapshots[dataset.dataset_id] = snapshot
                paths[dataset.dataset_id] = path
                with artifacts.prepare_source_snapshot(
                    selection.data_version_id
                ) as workspace:
                    candidate = workspace / "snapshot.parquet"
                    copyfile(path, candidate)
                    artifacts.publish_source_snapshot(
                        selection.data_version_id,
                        candidate,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )

            primary, first, second = selection.datasets
            request = ExactJoinGroupRequest(
                output_dataset_name="customers_complete",
                primary_dataset_id=primary.dataset_id,
                primary_key_column_keys=("key",),
                key_value_types=("integer",),
                additions=(
                    ExactJoinGroupAddition(first.dataset_id, ("key",)),
                    ExactJoinGroupAddition(second.dataset_id, ("key",)),
                ),
                projections=(
                    ExactJoinGroupProjection(primary.dataset_id, "key", "RecId"),
                    ExactJoinGroupProjection(primary.dataset_id, "field_2", "Name"),
                    ExactJoinGroupProjection(first.dataset_id, "field_3", "AddedOne"),
                    ExactJoinGroupProjection(second.dataset_id, "field_3", "AddedTwo"),
                ),
            )
            rules = DerivedEntityWorkspaceService._join_group_rules(
                request,
                selection,
                None,
            )
            evaluator = DuckDbExactJoinGroupEvaluator(
                database
            )

            preview = evaluator.review(
                request=request,
                rules=rules,
                selection=selection,
                snapshots=snapshots,
                paths=paths,
            )
            effective = structural_mapping_selection(selection, rules).datasets[-1]
            source = DuckDbGroupedExactJoinSource(
                database
            )
            batches = tuple(
                source.iter_batches(
                    group=BoundedExactJoinGroup(
                        rules=rules,
                        primary_dataset_id=primary.dataset_id,
                        participant_dataset_ids=(
                            primary.dataset_id,
                            first.dataset_id,
                            second.dataset_id,
                        ),
                        output_dataset_id=structural_dataset_id(rules[-1]),
                    ),
                    physical_selection=selection,
                    effective_dataset=effective,
                    snapshots=snapshots,
                    paths=paths,
                    batch_size=1_000,
                )
            )
            snapshot_evidence = tuple(
                ExactJoinInputSnapshotEvidence(
                    dataset_id=snapshot.dataset_id,
                    logical_hash=snapshot.logical_hash,
                    parquet_sha256=snapshot.parquet_sha256,
                )
                for snapshot in snapshots.values()
            )
            plan = DerivedEntityPlan(
                plan_id=str(uuid4()),
                version=1,
                workspace_id=str(uuid4()),
                source_selection_hash=selection.content_hash,
                rules=rules,
                updated_at=datetime.now(UTC),
                updated_by="Scale regression",
                dataset_uses=tuple(
                    SourceDatasetUsePolicy(
                        dataset.dataset_id,
                        SourceDatasetUse.PREPARATION_ONLY,
                    )
                    for dataset in selection.datasets
                ),
                join_reviews=tuple(
                    SavedExactJoinReview(
                        rule_id=rule.rule_id,
                        rule_hash=content_hash(rule.to_dict()),
                        source_selection_hash=selection.content_hash,
                        input_snapshots=snapshot_evidence,
                        review=review,
                    )
                    for rule, review in zip(rules, preview.reviews, strict=True)
                ),
            )
            effective_selection = mapping_source_selection(selection, plan)
            effective_dataset = effective_selection.datasets[0]
            by_name = {
                column.source_name: column for column in effective_dataset.columns
            }
            definition = MappingDefinition(
                mapping_id=str(uuid4()),
                source_selection_hash=effective_selection.content_hash,
                schema_hash="sha256:" + "9" * 64,
                datasets=(
                    DatasetMapping(
                        dataset_id=effective_dataset.dataset_id,
                        target_model="x_generic.record",
                        source_identity_column_keys=(
                            by_name["RecId"].stable_key,
                        ),
                        target_identity=(
                            IdentityComponentMapping(
                                source_column_keys=(
                                    by_name["RecId"].stable_key,
                                ),
                                target_fields=("external_key",),
                                value_type="integer",
                            ),
                        ),
                        fields=(
                            ScalarFieldMapping(
                                target_field="display_name",
                                source_column_key=by_name["Name"].stable_key,
                            ),
                            ScalarFieldMapping(
                                target_field="added_one",
                                source_column_key=by_name["AddedOne"].stable_key,
                            ),
                            ScalarFieldMapping(
                                target_field="added_two",
                                source_column_key=by_name["AddedTwo"].stable_key,
                            ),
                        ),
                    ),
                ),
            )
            workspace = WorkspaceState(
                workspace_id=plan.workspace_id,
                name="Grouped scale regression",
                source_system="CSV",
                status=WorkspaceStatus.REGISTERED,
                registered_at=datetime.now(UTC),
            )
            WorkspaceStateRepository(database).initialize_workbench(
                workspace,
                actor=LOCAL_ACTOR,
            )
            capability = compile_preparation_capability(
                definition=definition,
                physical_selection=selection,
                effective_selection=effective_selection,
                source_snapshots=tuple(snapshots.values()),
                derived_plan=plan,
                current_ruleset=None,
                reference_bundle=None,
            )
            self.assertTrue(capability.admitted)
            self.assertFalse(capability.permits_materialized_fallback)
            prepared = prepare_bounded_grouped_exact_join_session(
                workspace,
                definition,
                1,
                selection,
                effective_selection,
                plan,
                artifacts,
                None,
                PreparationSessionRepository(database, artifacts),
                source,
                actor=LOCAL_ACTOR,
                source_snapshots=tuple(snapshots.values()),
            )

        self.assertEqual(sum(_ROW_COUNTS), 65_299)
        self.assertEqual(sum(_FIELD_COUNTS), 262)
        self.assertEqual(sum(_EMPTY_FIELD_COUNTS), 97)
        self.assertTrue(preview.can_save)
        self.assertTrue(preview.bounded_execution_plan_verified)
        self.assertEqual(preview.physical_snapshot_scans, 3)
        self.assertLessEqual(preview.statement_count, 12)
        self.assertEqual(
            tuple(review.actual_output_rows for review in preview.reviews),
            (_ROW_COUNTS[0], _ROW_COUNTS[0]),
        )
        streamed_rows = tuple(row for batch in batches for row in batch.rows)
        streamed_lineage = tuple(
            lineage for batch in batches for lineage in batch.physical_sources
        )
        self.assertEqual(len(streamed_rows), _ROW_COUNTS[0])
        self.assertEqual(streamed_rows[0].values["RecId"], 1)
        self.assertEqual(streamed_rows[0].values["AddedOne"], "value-3")
        self.assertEqual(streamed_rows[0].values["AddedTwo"], "value-3")
        self.assertTrue(
            all(len(lineage) == 3 for lineage in streamed_lineage)
        )
        self.assertEqual(len(prepared.run.rows), _ROW_COUNTS[0])
        self.assertEqual(prepared.run.datasets[0].input_rows, sum(_ROW_COUNTS))
        self.assertEqual(
            prepared.run.datasets[0].input_rows_used,
            sum(_ROW_COUNTS) - 1,
        )
        self.assertEqual(prepared.run.datasets[0].unrepresented_rows, 1)


def _selection() -> SourceSelection:
    datasets = tuple(
        _dataset(index, row_count, field_count, empty_count)
        for index, (row_count, field_count, empty_count) in enumerate(
            zip(_ROW_COUNTS, _FIELD_COUNTS, _EMPTY_FIELD_COUNTS, strict=True),
            start=1,
        )
    )
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=datetime.now(UTC),
        created_by="Scale regression",
        datasets=datasets,
        content_hash="sha256:" + "3" * 64,
    )


def _dataset(
    index: int,
    row_count: int,
    field_count: int,
    empty_count: int,
) -> SourceDataset:
    empty_start = field_count - empty_count + 1
    columns = tuple(
        SourceDatasetColumn(
            ordinal,
            (
                "RecId"
                if ordinal == 1
                else "Name"
                if ordinal == 2
                else f"Table{index}Field{ordinal}"
            ),
            "key" if ordinal == 1 else f"field_{ordinal}",
            "empty" if ordinal >= empty_start else "string",
        )
        for ordinal in range(1, field_count + 1)
    )
    return SourceDataset(
        dataset_id=f"dataset:{index:024x}",
        name=f"customer_source_{index}",
        source=FileSourceBinding(
            file_id=str(uuid4()),
            table_key=f"customer_source_{index}.csv",
            source_sha256=f"{index:064x}",
            catalog_hash="sha256:" + f"{index + 10:064x}",
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=row_count,
        columns=columns,
    )


def _write_wide_snapshot(path: Path, dataset: SourceDataset) -> None:
    schema = source_snapshot_schema(dataset)
    source_row = schema.source_row_column
    frame = pl.DataFrame(
        {source_row: range(1, dataset.row_count + 1)},
        schema={source_row: pl.Int64},
    ).lazy()
    expressions: list[pl.Expr] = []
    for column in schema.columns:
        if column.ordinal == 1:
            value = pl.col(source_row).cast(pl.String)
            kind = pl.lit(int(SourceCellKind.INTEGER), dtype=pl.UInt8)
        elif column.candidate_type == "empty":
            value = pl.lit(None, dtype=pl.String)
            kind = pl.lit(int(SourceCellKind.NULL), dtype=pl.UInt8)
        else:
            value = pl.lit(f"value-{column.ordinal}", dtype=pl.String)
            kind = pl.lit(int(SourceCellKind.STRING), dtype=pl.UInt8)
        expressions.extend(
            (
                value.alias(column.value_column),
                kind.alias(column.kind_column),
            )
        )
    frame.with_columns(expressions).select(
        source_row,
        *(
            name
            for column in schema.columns
            for name in (column.value_column, column.kind_column)
        ),
    ).sink_parquet(
        path,
        compression="zstd",
        maintain_order=True,
        engine="streaming",
    )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
