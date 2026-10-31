"""Stream a qualified grouped exact join from immutable Parquet snapshots.

Every participating snapshot is projected once into a narrow temporary table.
The final left-deep join reads only those narrow tables and is fetched in
bounded pages.  Connections are request-scoped and are never cached.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import duckdb

from impodo.application.shared.columnar_runtime import configure_columnar_runtime
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
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    DerivedEntityRule,
    HierarchyConsumerBinding,
    hierarchy_consumer_values,
)

from .database import DuckDbWorkspaceDatabase
from .exact_join_group_evaluator import parsed_join_key_expression
from .lookup_preparation_source import iter_lookup_batches

configure_columnar_runtime()

import polars as pl

_SOURCE_MEMORY_LIMIT = "256MB"
_SOURCE_THREADS = "2"
# Match Python str.strip(), including Unicode and control whitespace.
_CHOICE_WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


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
    hierarchies: tuple[HierarchyConsumerBinding, ...] = ()


class DuckDbGroupedExactJoinSource(GroupedExactJoinSourcePort):
    """Execute one bounded grouped join using one local DuckDB connection."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        self._connection_factory = database.connection_factory

    def iter_lookup_batches(
        self, *, rule: DerivedEntityRule, link: DerivedDatasetLink,
        effective_dataset: SourceDataset, snapshot: SourceSnapshot, path: Path,
        batch_size: int,
    ) -> Iterator[GroupedExactJoinBatch]:
        """Delegate one narrow lookup producer within the same worker boundary."""

        yield from iter_lookup_batches(self._connection_factory, rule=rule, link=link,
            effective_dataset=effective_dataset, snapshot=snapshot, path=path,
            batch_size=batch_size)

    def project_values(
        self,
        *,
        group: BoundedExactJoinGroup,
        physical_selection: SourceSelection,
        effective_dataset: SourceDataset,
        snapshots: Mapping[str, SourceSnapshot],
        paths: Mapping[str, Path],
        column_keys: Sequence[str],
        maximum_values: int | None = None,
    ) -> pl.DataFrame:
        """Project narrow joined values, optionally aggregating choices in SQL.

        Raw snapshot value strings preserve the categorical scanner's existing
        semantics. No rich rows or unrelated output columns are materialized.
        """

        keys = tuple(column_keys)
        columns = {column.stable_key: column for column in effective_dataset.columns}
        if (
            not keys
            or len(set(keys)) != len(keys)
            or any(key not in columns for key in keys)
        ):
            raise SourceLoadError("Choose current logical source columns")
        if maximum_values is not None and (len(keys) != 1 or maximum_values < 1):
            raise ValueError("Choice aggregation requires one column and a positive limit")
        shape = _execution_shape(group, physical_selection, effective_dataset, keys)
        needed = _needed_columns(shape)
        with self._connection_factory.connect(
            Path(":memory:"),
            memory_limit=_SOURCE_MEMORY_LIMIT,
            threads=_SOURCE_THREADS,
            preserve_insertion_order=False,
            enable_external_access=True,
        ) as connection:
            try:
                tables = {}
                for index, dataset_id in enumerate(shape.participant_ids):
                    table = f"_impodo_grouped_values_{index}"
                    _create_narrow_input(
                        connection,
                        table_name=table,
                        path=paths[dataset_id],
                        snapshot=snapshots[dataset_id],
                        needed_column_keys=needed[dataset_id],
                        key_column_keys=shape.keys_by_dataset[dataset_id],
                        key_value_types=shape.key_value_types,
                    )
                    tables[dataset_id] = table
                if shape.hierarchies:
                    cursor = connection.execute(_joined_query(shape, snapshots, tables))
                    projected: list[tuple[object, ...]] = []
                    counts: Counter[str] = Counter()
                    ordinary = {item.output_name: index for index, item in enumerate(shape.projections)}
                    total_rows = 0
                    virtual = {binding.link.source_column_key for binding in shape.hierarchies}
                    while batch := cursor.fetchmany(1_000):
                        for raw in batch:
                            total_rows += 1
                            source_rows = raw[:len(shape.participant_ids)]
                            raw_values = raw[len(shape.participant_ids):]
                            values = _decode_values(raw_values, shape.projections)
                            values.update(_hierarchy_values(shape, values, source_rows))
                            row = tuple(
                                values[key] if key in virtual
                                else raw_values[ordinary[columns[key].source_name] * 2]
                                for key in keys
                            )
                            if maximum_values is None:
                                projected.append(row)
                            elif row[0] is not None and (value := str(row[0]).strip()):
                                counts[value] += 1
                    if total_rows != effective_dataset.row_count:
                        raise SourceLoadError("The reviewed joined row count changed")
                    if maximum_values is not None:
                        return pl.DataFrame(sorted(counts.items())[:maximum_values + 1], schema=["value", "count"], orient="row")
                    return pl.DataFrame(projected, schema=list(keys), orient="row")
                query = _joined_query(
                    shape, snapshots, tables,
                    value_keys=keys,
                    ordered=maximum_values is None,
                )
                parameters = []
                if maximum_values is not None:
                    query = f"""
                        WITH choices AS (
                            SELECT trim({_identifier(keys[0])}, ?) AS value
                              FROM ({query}) AS joined_values
                        )
                        SELECT value, count(*) AS count
                          FROM choices
                         WHERE value IS NOT NULL AND value <> ''
                         GROUP BY 1
                         ORDER BY 1
                         LIMIT {maximum_values + 1}
                    """
                    parameters = [_CHOICE_WHITESPACE]
                    result_keys = ("value", "count")
                else:
                    result_keys = keys
                rows = connection.execute(query, parameters).fetchall()
                if maximum_values is None and len(rows) != effective_dataset.row_count:
                    raise SourceLoadError("The reviewed joined row count changed")
                return pl.DataFrame(rows, schema=list(result_keys), orient="row")
            except duckdb.Error as error:
                raise SourceLoadError("Impodo could not read the reviewed joined values") from error

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
                        carried_values = _hierarchy_values(shape, values, source_rows)
                        for binding in shape.hierarchies:
                            name = next(column.source_name for column in effective_dataset.columns
                                        if column.stable_key == binding.link.source_column_key)
                            values[name] = carried_values.pop(binding.link.source_column_key)
                        values.update(carried_values)
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
    column_keys: tuple[str, ...] | None = None,
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
    requested = set(column_keys) if column_keys is not None else {
        column.stable_key for column in effective_dataset.columns
    }
    virtual = {binding.link.source_column_key for binding in group.hierarchies}
    hierarchies = tuple(binding for binding in group.hierarchies
                        if binding.link.source_column_key in requested)
    try:
        by_key = {column.stable_key: column for column in effective_dataset.columns}
        projections = tuple(
            _JoinProjection(by_key[key].source_name, final_origins[key])
            for key in (column_keys if column_keys is not None else tuple(by_key))
            if key not in virtual
        ) + tuple(
            _JoinProjection(carried_key, _PhysicalOrigin(binding.rule.source_dataset_id, physical_key))
            for binding in hierarchies
            for physical_key, carried_key in zip(
                binding.rule.source_level_column_keys, binding.link.source_level_column_keys, strict=True
            )
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
        hierarchies=hierarchies,
    )


def _hierarchy_values(
    shape: _ExecutionShape, values: Mapping[str, object], source_rows: Sequence[object],
) -> dict[str, object]:
    """Keep actual blank inputs distinct from an unmatched optional enrichment."""
    result: dict[str, object] = {}
    for binding in shape.hierarchies:
        present = source_rows[shape.participant_ids.index(binding.rule.source_dataset_id)] is not None
        source = {
            physical_key: values[carried_key]
            for physical_key, carried_key in zip(binding.rule.source_level_column_keys,
                                                binding.link.source_level_column_keys, strict=True)
        } if present else None
        result.update(hierarchy_consumer_values(binding, source))
    return result


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
    *,
    value_keys: tuple[str, ...] | None = None,
    ordered: bool = True,
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
    for index, projection in enumerate(shape.projections):
        snapshot_columns = {
            column.stable_key: column
            for column in snapshots[projection.origin.dataset_id].schema.columns
        }
        column = snapshot_columns[projection.origin.column_key]
        alias = aliases[projection.origin.dataset_id]
        if value_keys is None:
            value_items.extend(
                (f"{alias}.value_{column.ordinal}", f"{alias}.kind_{column.ordinal}")
            )
        else:
            value_items.append(
                f"{alias}.value_{column.ordinal} AS {_identifier(value_keys[index])}"
            )
    selected = ", ".join(value_items)
    if value_keys is None:
        selected = ", ".join(part for part in (source_rows, selected) if part)
    return f"""
        SELECT {selected}
          FROM {tables[shape.primary_dataset_id]} AS {primary_alias}
          {" ".join(joins)}
         {f'ORDER BY {primary_alias}.source_row' if ordered else ''}
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
