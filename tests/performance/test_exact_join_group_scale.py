from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import polars as pl

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.exact_join_group_evaluator import (
    DuckDbExactJoinGroupEvaluator,
)
from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.derived_entities import (
    DerivedEntityWorkspaceService,
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import SourceCellKind, SourceSnapshot
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

_ROW_COUNTS = (21_766, 21_766, 21_767)
_FIELD_COUNTS = (88, 87, 87)
_EMPTY_FIELD_COUNTS = (33, 32, 32)


class ExactJoinGroupScaleTests(unittest.TestCase):
    def test_workspace_shaped_review_scans_each_wide_snapshot_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection()
            snapshots: dict[str, SourceSnapshot] = {}
            paths: dict[str, Path] = {}
            for dataset in selection.datasets:
                path = root / f"{dataset.name}.parquet"
                _write_wide_snapshot(path, dataset)
                schema = source_snapshot_schema(dataset)
                snapshots[dataset.dataset_id] = SourceSnapshot.create(
                    data_version_id=selection.data_version_id,
                    dataset_id=dataset.dataset_id,
                    dataset_name=dataset.name,
                    source=dataset.source,
                    physical_selection_hash=selection.content_hash,
                    schema=schema,
                    row_count=dataset.row_count,
                    data_logical_hash="sha256:" + "4" * 64,
                    parquet_sha256="sha256:" + "5" * 64,
                    created_at=selection.created_at,
                )
                paths[dataset.dataset_id] = path

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
                DuckDbWorkspaceDatabase(root / "workspace-databases")
            )

            preview = evaluator.review(
                request=request,
                rules=rules,
                selection=selection,
                snapshots=snapshots,
                paths=paths,
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


if __name__ == "__main__":
    unittest.main()
