"""Stream a qualified grouped exact join from immutable Parquet snapshots.

Every participating snapshot is projected once into a narrow temporary table.
The final left-deep join reads only those narrow tables and is fetched in
bounded pages.  Connections are request-scoped and are never cached.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import duckdb

from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    BoundedExactJoinGroup,
    GroupedExactJoinBatch,
    GroupedExactJoinSourcePort,
)
from impodo.domain.preparation.source import SourceLoadError, SourceRow
from impodo.domain.source_snapshot import (
    EncodedSourceCell,
    SourceCellKind,
    SourceSnapshot,
    SourceSnapshotContractError,
)
from impodo.domain.structural import structural_dataset_id
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection

from .database import DuckDbWorkspaceDatabase
from .exact_join_group_evaluator import parsed_join_key_expression

_SOURCE_MEMORY_LIMIT = "256MB"
_SOURCE_THREADS = "2"


@dataclass(frozen=True, slots=True)
class _PhysicalOrigin:
    dataset_id: str
    column_key: str


@dataclass(frozen=True, slots=True)
class _JoinProjection:
    output_name: str
    origin: _PhysicalOrigin


@dataclass(frozen=True, slots=True)
class _ExecutionShape:
    primary_dataset_id: str
    participant_ids: tuple[str, ...]
    keys_by_dataset: Mapping[str, tuple[str, ...]]
    key_value_types: tuple[str, ...]
    projections: tuple[_JoinProjection, ...]


class DuckDbGroupedExactJoinSource(GroupedExactJoinSourcePort):
    """Execute one bounded grouped join using one local DuckDB connection."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        self._connection_factory = database.connection_factory

    def iter_batches(
        self,
        *,
        group: BoundedExactJoinGroup,
        physical_selection: SourceSelection,
        effective_dataset: SourceDataset,
        snapshots: Mapping[str, SourceSnapshot],
        paths: Mapping[str, Path],
        batch_size: int,
    ) -> Iterator[GroupedExactJoinBatch]:
        """Scan each input once, join narrow tables, and yield decoded pages."""

        if batch_size < 1:
            raise ValueError("Grouped exact-join batch size must be positive")
        shape = _execution_shape(group, physical_selection, effective_dataset)
        if set(paths) != set(shape.participant_ids) or not set(
            shape.participant_ids
        ).issubset(snapshots):
            raise SourceLoadError("Grouped exact-join snapshot evidence is incomplete")
        datasets = {item.dataset_id: item for item in physical_selection.datasets}
        if not set(shape.participant_ids).issubset(datasets):
            raise SourceLoadError("Grouped exact-join source evidence is incomplete")

        with self._connection_factory.connect(
            Path(":memory:"),
            memory_limit=_SOURCE_MEMORY_LIMIT,
            threads=_SOURCE_THREADS,
            preserve_insertion_order=False,
            enable_external_access=True,
        ) as connection:
            try:
                aliases: dict[str, str] = {}
                needed_by_dataset = _needed_columns(shape)
                for index, dataset_id in enumerate(shape.participant_ids):
                    table_name = f"_impodo_grouped_source_{index}"
                    _create_narrow_input(
                        connection,
                        table_name=table_name,
                        path=paths[dataset_id],
                        snapshot=snapshots[dataset_id],
                        needed_column_keys=needed_by_dataset[dataset_id],
                        key_column_keys=shape.keys_by_dataset[dataset_id],
                        key_value_types=shape.key_value_types,
                    )
                    aliases[dataset_id] = table_name
                cursor = connection.execute(
                    _joined_query(shape, snapshots, aliases)
                )
                output_number = 0
                while raw_batch := cursor.fetchmany(batch_size):
                    rows: list[SourceRow] = []
                    lineages: list[Mapping[str, tuple[int, ...]]] = []
                    for raw in raw_batch:
                        output_number += 1
                        source_rows = raw[: len(shape.participant_ids)]
                        values = _decode_values(
                            raw[len(shape.participant_ids) :],
                            shape.projections,
                        )
                        rows.append(SourceRow(output_number, values))
                        lineages.append(
                            {
                                dataset_id: (int(source_row),)
                                for dataset_id, source_row in zip(
                                    shape.participant_ids,
                                    source_rows,
                                    strict=True,
                                )
                                if source_row is not None
                            }
                        )
                    yield GroupedExactJoinBatch(tuple(rows), tuple(lineages))
            except duckdb.Error as error:
                raise SourceLoadError(
                    "Impodo could not stream the reviewed grouped exact join"
                ) from error


def _execution_shape(
    group: BoundedExactJoinGroup,
    physical_selection: SourceSelection,
    effective_dataset: SourceDataset,
) -> _ExecutionShape:
    physical = {dataset.dataset_id: dataset for dataset in physical_selection.datasets}
    origins: dict[str, dict[str, _PhysicalOrigin]] = {
        dataset_id: {
            column.stable_key: _PhysicalOrigin(dataset_id, column.stable_key)
            for column in dataset.columns
        }
        for dataset_id, dataset in physical.items()
    }
    keys_by_dataset: dict[str, tuple[str, ...]] = {}
    key_value_types: tuple[str, ...] | None = None
    for rule in group.rules:
        try:
            left_origins = origins[rule.left_dataset_id]
            right_origins = origins[rule.right_dataset_id]
            left_keys = tuple(left_origins[key.left_column_key] for key in rule.keys)
            right_keys = tuple(right_origins[key.right_column_key] for key in rule.keys)
        except KeyError as error:
            raise SourceLoadError("Grouped exact-join fields changed") from error
        if any(origin.dataset_id != group.primary_dataset_id for origin in left_keys):
            raise SourceLoadError("Grouped exact-join base keys changed")
        keys_by_dataset[group.primary_dataset_id] = tuple(
            origin.column_key for origin in left_keys
        )
        keys_by_dataset[rule.right_dataset_id] = tuple(
            origin.column_key for origin in right_keys
        )
        current_types = tuple(key.value_type for key in rule.keys)
        if key_value_types is None:
            key_value_types = current_types
        elif current_types != key_value_types:
            raise SourceLoadError("Grouped exact-join key types changed")
        output_origins: dict[str, _PhysicalOrigin] = {}
        for projection in rule.projections:
            try:
                output_origins[projection.output_column_key] = origins[
                    projection.source_dataset_id
                ][projection.source_column_key]
            except KeyError as error:
                raise SourceLoadError("Grouped exact-join projection changed") from error
        origins[structural_dataset_id(rule)] = output_origins

    final_origins = origins.get(group.output_dataset_id)
    if final_origins is None:
        raise SourceLoadError("Grouped exact-join output changed")
    try:
        projections = tuple(
            _JoinProjection(column.source_name, final_origins[column.stable_key])
            for column in effective_dataset.columns
        )
    except KeyError as error:
        raise SourceLoadError("Grouped exact-join output fields changed") from error
    if key_value_types is None:
        raise SourceLoadError("Grouped exact-join keys are unavailable")
    return _ExecutionShape(
        primary_dataset_id=group.primary_dataset_id,
        participant_ids=group.participant_dataset_ids,
        keys_by_dataset=keys_by_dataset,
        key_value_types=key_value_types,
        projections=projections,
    )


def _needed_columns(shape: _ExecutionShape) -> dict[str, tuple[str, ...]]:
    needed = {
        dataset_id: list(shape.keys_by_dataset[dataset_id])
        for dataset_id in shape.participant_ids
    }
    for projection in shape.projections:
        values = needed[projection.origin.dataset_id]
        if projection.origin.column_key not in values:
            values.append(projection.origin.column_key)
    return {dataset_id: tuple(values) for dataset_id, values in needed.items()}


def _create_narrow_input(
    connection: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    path: Path,
    snapshot: SourceSnapshot,
    needed_column_keys: tuple[str, ...],
    key_column_keys: tuple[str, ...],
    key_value_types: tuple[str, ...],
) -> None:
    columns = {column.stable_key: column for column in snapshot.schema.columns}
    if any(key not in columns for key in needed_column_keys):
        raise SourceLoadError("Grouped exact-join snapshot fields changed")
    source_items = [_identifier(snapshot.schema.source_row_column) + " AS source_row"]
    projected_items = ["source_row"]
    for column_key in needed_column_keys:
        column = columns[column_key]
        source_items.extend(
            (
                f"{_identifier(column.value_column)} AS value_{column.ordinal}",
                f"{_identifier(column.kind_column)} AS kind_{column.ordinal}",
            )
        )
        projected_items.extend((f"value_{column.ordinal}", f"kind_{column.ordinal}"))
    key_items = tuple(
        parsed_join_key_expression(
            f"value_{columns[column_key].ordinal}",
            value_type,
        )
        + f" AS join_key_{index}"
        for index, (column_key, value_type) in enumerate(
            zip(key_column_keys, key_value_types, strict=True),
            start=1,
        )
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE {table_name} AS
        WITH source AS (
            SELECT {", ".join(source_items)}
              FROM read_parquet(?)
        )
        SELECT {", ".join(projected_items)},
               {", ".join(key_items)}
          FROM source
        """,
        [str(path.resolve())],
    )
    count = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    if count is None or int(count[0]) != snapshot.row_count:
        raise SourceLoadError("Grouped exact-join snapshot row count changed")


def _joined_query(
    shape: _ExecutionShape,
    snapshots: Mapping[str, SourceSnapshot],
    tables: Mapping[str, str],
) -> str:
    aliases = {
        dataset_id: f"input_{index}"
        for index, dataset_id in enumerate(shape.participant_ids)
    }
    primary_alias = aliases[shape.primary_dataset_id]
    joins = []
    for dataset_id in shape.participant_ids[1:]:
        alias = aliases[dataset_id]
        condition = " AND ".join(
            f"{primary_alias}.join_key_{index} = {alias}.join_key_{index}"
            for index in range(1, len(shape.key_value_types) + 1)
        )
        joins.append(
            f"LEFT JOIN {tables[dataset_id]} AS {alias} ON {condition}"
        )
    source_rows = ", ".join(
        f"{aliases[dataset_id]}.source_row"
        for dataset_id in shape.participant_ids
    )
    value_items: list[str] = []
    for projection in shape.projections:
        snapshot_columns = {
            column.stable_key: column
            for column in snapshots[projection.origin.dataset_id].schema.columns
        }
        column = snapshot_columns[projection.origin.column_key]
        alias = aliases[projection.origin.dataset_id]
        value_items.extend(
            (f"{alias}.value_{column.ordinal}", f"{alias}.kind_{column.ordinal}")
        )
    return f"""
        SELECT {source_rows}, {", ".join(value_items)}
          FROM {tables[shape.primary_dataset_id]} AS {primary_alias}
          {" ".join(joins)}
         ORDER BY {primary_alias}.source_row
    """


def _decode_values(
    raw_values: tuple[object, ...],
    projections: tuple[_JoinProjection, ...],
) -> dict[str, object]:
    values: dict[str, object] = {}
    for index, projection in enumerate(projections):
        text = raw_values[index * 2]
        raw_kind = raw_values[index * 2 + 1]
        try:
            cell = (
                EncodedSourceCell(SourceCellKind.NULL, None)
                if raw_kind is None
                else EncodedSourceCell(
                    SourceCellKind(cast(int, raw_kind)),
                    str(text) if text is not None else None,
                )
            )
            values[projection.output_name] = cell.to_python()
        except (TypeError, ValueError, SourceSnapshotContractError) as error:
            raise SourceLoadError(
                "Grouped exact-join snapshot contains an invalid value"
            ) from error
    return values


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
