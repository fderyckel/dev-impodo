"""Read generated lists and hierarchies from narrow frozen Parquet columns.

DuckDB groups encoded input paths before Python applies the shared domain
policies. Mapping reads return only requested values; preparation additionally
streams complete physical membership through the same generated records.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from impodo.application.shared.columnar_runtime import configure_columnar_runtime

configure_columnar_runtime()

import duckdb
import polars as pl

from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    GroupedExactJoinBatch,
)
from impodo.domain.preparation.source import SourceLoadError, SourceRow
from impodo.domain.shared.models import Issue, Severity
from impodo.domain.source_snapshot import (
    SOURCE_ROW_COLUMN,
    EncodedSourceCell,
    SourceCellKind,
    SourceSnapshot,
)
from impodo.domain.staging.scale import LOOKUP_DERIVED_OUTPUT_ROW_LIMIT
from impodo.domain.workspace.contracts import SourceDataset
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    HierarchicalLookupRule,
    HierarchyConsumerBinding,
    LookupRule,
    _display_path,
    _normalized_path,
    evaluate_hierarchy_path,
    hierarchy_consumer_values,
    hierarchy_path_key,
)

from .constants import DUCKDB_JSON_BATCH_MAX_BYTES
from .serialization import iter_encoded_json_batches
from .unit_of_work import DuckDbConnectionFactory

_LOOKUP_LINEAGE_BATCH_BYTES = 32 * 1024 * 1024
_NORMALIZED_SCHEMA = (
    '[{"raw_id":"BIGINT","canonical_parts":"VARCHAR[]",'
    '"canonical_key":"VARCHAR","parent_key":"VARCHAR","name":"VARCHAR",'
    '"alias":"VARCHAR","company":"VARCHAR"}]'
)
_CHOICE_WHITESPACE = "".join(
    chr(value) for value in range(0x3100) if chr(value).isspace()
)


def project_hierarchy_consumer_values(
    factory: DuckDbConnectionFactory,
    *,
    bindings: Sequence[HierarchyConsumerBinding],
    snapshot: SourceSnapshot,
    path: Path,
    column_keys: Sequence[str],
    maximum_values: int | None = None,
) -> pl.DataFrame:
    """Read physical values and generated parent paths in original row order."""
    columns = {item.stable_key: item for item in snapshot.schema.columns}
    virtual = {item.link.source_column_key: item for item in bindings}
    keys = tuple(column_keys)
    if not keys or len(set(keys)) != len(keys) or any(
        key not in columns and key not in virtual and key != SOURCE_ROW_COLUMN
        for key in keys
    ):
        raise SourceLoadError("Choose current hierarchy source columns")
    if maximum_values is not None and (len(keys) != 1 or maximum_values < 1):
        raise SourceLoadError("Choose one hierarchy source column and a positive limit")
    active = tuple(binding for key, binding in virtual.items() if key in keys)
    levels = tuple(dict.fromkeys(
        key for binding in active for key in binding.rule.source_level_column_keys
    ))
    needed = tuple(dict.fromkeys((*levels, *(key for key in keys if key in columns))))
    if any(key not in columns for key in needed):
        raise SourceLoadError("The hierarchy's frozen source fields changed")
    projected = ", ".join(
        f'"{columns[key].kind_column}", "{columns[key].value_column}"'
        for key in needed
    )
    rows: list[tuple[object, ...]] = []
    counts: Counter[str] = Counter()
    total = 0
    previous_number = 0
    with factory.connect(
        Path(":memory:"), memory_limit="256MB", threads="2",
        preserve_insertion_order=False, enable_external_access=True,
    ) as connection:
        try:
            cursor = connection.execute(
                f'SELECT "{snapshot.schema.source_row_column}"'
                f'{", " + projected if projected else ""} FROM read_parquet(?) '
                f'ORDER BY "{snapshot.schema.source_row_column}"',
                [str(path.resolve())],
            )
            while batch := cursor.fetchmany(1000):
                for number, *cells in batch:
                    if number is None or int(number) <= previous_number:
                        raise SourceLoadError("The hierarchy's frozen source rows changed")
                    previous_number = int(number)
                    total += 1
                    raw = {key: cells[index * 2 + 1] for index, key in enumerate(needed)}
                    decoded = {
                        key: EncodedSourceCell(
                            SourceCellKind(int(cells[index * 2])), cells[index * 2 + 1]
                        ).to_python()
                        for index, key in enumerate(needed) if key in levels
                    }
                    values: dict[str, object] = {**raw, SOURCE_ROW_COLUMN: int(number)}
                    for binding in active:
                        values[binding.link.source_column_key] = hierarchy_consumer_values(
                            binding, decoded
                        )[binding.link.source_column_key]
                    row = tuple(values[key] for key in keys)
                    if maximum_values is None:
                        rows.append(row)
                    elif row[0] is not None and (value := str(row[0]).strip()):
                        counts[value] += 1
            if total != snapshot.row_count:
                raise SourceLoadError("The hierarchy's frozen source row count changed")
        except duckdb.Error as error:
            raise SourceLoadError("Impodo could not read the hierarchy's source values") from error
    if maximum_values is not None:
        return pl.DataFrame(
            sorted(counts.items())[:maximum_values + 1],
            schema={keys[0]: pl.String, "count": pl.Int64}, orient="row",
        )
    return pl.DataFrame(
        rows, schema={key: pl.Int64 if key == SOURCE_ROW_COLUMN else pl.String for key in keys},
        orient="row",
    )


def _source_keys(rule: LookupRule) -> tuple[str, ...]:
    """Select only fields that determine the generated path."""
    return (
        rule.source_level_column_keys
        if isinstance(rule, HierarchicalLookupRule)
        else (rule.source_column_key,)
    )


def _normalized_nodes(
    rule: LookupRule, raw_id: int, cells: Sequence[Any]
) -> list[dict[str, object]]:
    """Apply authoritative Unicode and missing-level policies once per raw path."""
    decoded = tuple(
        EncodedSourceCell(
            SourceCellKind(int(cells[index])), cells[index + 1]
        ).to_python()
        for index in range(0, len(cells), 2)
    )
    if isinstance(rule, HierarchicalLookupRule):
        evaluation = evaluate_hierarchy_path(
            rule, dict(zip(_source_keys(rule), decoded, strict=True))
        )
        display, canonical = evaluation.display_parts, evaluation.canonical_parts
        separator = "/"
    else:
        path = _normalized_path(decoded[0], rule.parent_separator)
        if path is None:
            return []
        display, canonical = path
        separator = rule.parent_separator
    return [
        {
            "raw_id": raw_id,
            "canonical_parts": canonical[:depth],
            "canonical_key": hierarchy_path_key(rule, canonical[:depth]),
            "parent_key": hierarchy_path_key(rule, canonical[: depth - 1])
            if depth > 1
            else None,
            "name": display[depth - 1],
            "alias": _display_path(display[:depth], separator),
            "company": rule.existing_parent.company_for_depth(depth)
            if isinstance(rule, HierarchicalLookupRule)
            and rule.existing_parent is not None
            else None,
        }
        for depth in range(1, len(canonical) + 1)
    ]


@contextmanager
def _lookup_connection(
    factory: DuckDbConnectionFactory,
    *,
    rule: LookupRule,
    snapshot: SourceSnapshot,
    path: Path,
    batch_size: int,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Own one bounded database and one input scan for a generated-table read."""
    keys = _source_keys(rule)
    columns = {item.stable_key: item for item in snapshot.schema.columns}
    if (
        batch_size < 1
        or snapshot.dataset_id != rule.source_dataset_id
        or any(key not in columns for key in keys)
    ):
        raise SourceLoadError("The generated table's frozen source fields changed")
    projected = ", ".join(
        f'"{columns[key].kind_column}" AS kind_{index}, "{columns[key].value_column}" AS value_{index}'
        for index, key in enumerate(keys)
    )
    names = ", ".join(f"kind_{index}, value_{index}" for index in range(len(keys)))
    with factory.connect(
        Path(":memory:"),
        memory_limit="256MB",
        threads="2",
        preserve_insertion_order=False,
        enable_external_access=True,
    ) as connection:
        try:
            connection.execute(
                f'CREATE TEMP TABLE lookup_input AS SELECT "{snapshot.schema.source_row_column}" AS source_row, '
                f"{projected} FROM read_parquet(?)",
                [str(path.resolve())],
            )
            count = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT source_row), MIN(source_row) FROM lookup_input"
            ).fetchone()
            if (
                count is None
                or int(count[0]) != snapshot.row_count
                or int(count[1]) != snapshot.row_count
                or (count[2] is not None and int(count[2]) < 1)
            ):
                raise SourceLoadError(
                    "The generated table's frozen source rows changed"
                )
            connection.execute(
                "CREATE TEMP TABLE lookup_distinct AS SELECT ROW_NUMBER() OVER (ORDER BY MIN(source_row)) AS raw_id, "
                f"MIN(source_row) AS first_source_row, {names} FROM lookup_input GROUP BY {names}"
            )
            connection.execute(
                "CREATE TEMP TABLE lookup_normalized (raw_id BIGINT, canonical_parts VARCHAR[], "
                "canonical_key VARCHAR, parent_key VARCHAR, name VARCHAR, alias VARCHAR, company VARCHAR)"
            )
            offset = 0
            while raw_batch := connection.execute(
                f"SELECT raw_id, {names} FROM lookup_distinct WHERE raw_id > ? ORDER BY raw_id LIMIT ?",
                [offset, batch_size],
            ).fetchall():
                nodes = [
                    node
                    for raw_id, *cells in raw_batch
                    for node in _normalized_nodes(rule, int(raw_id), cells)
                ]
                for encoded in iter_encoded_json_batches(
                    nodes, max_rows=batch_size, max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES
                ):
                    connection.execute(
                        "INSERT INTO lookup_normalized SELECT item.raw_id, item.canonical_parts, item.canonical_key, "
                        "item.parent_key, item.name, item.alias, item.company "
                        "FROM (SELECT UNNEST(from_json_strict(CAST(? AS JSON), ?)) AS item)",
                        [encoded.payload, _NORMALIZED_SCHEMA],
                    )
                offset = int(raw_batch[-1][0])
            connection.execute("""
                CREATE TEMP TABLE lookup_records AS
                SELECT ROW_NUMBER() OVER (ORDER BY len(canonical_parts), canonical_parts) + 1 AS generated_row,
                       canonical_parts, canonical_key, parent_key, company,
                       arg_min(name, first_source_row) AS name,
                       COUNT(DISTINCT alias) AS aliases, MIN(first_source_row) AS first_source_row
                  FROM lookup_normalized JOIN lookup_distinct USING (raw_id)
                 GROUP BY canonical_parts, canonical_key, parent_key, company
            """)
            groups = connection.execute(
                "SELECT COUNT(*) FROM lookup_records"
            ).fetchone()
            if groups is None or int(groups[0]) > LOOKUP_DERIVED_OUTPUT_ROW_LIMIT:
                raise SourceLoadError(
                    "The generated table exceeds its bounded generated-record budget"
                )
            yield connection
        except duckdb.Error as error:
            raise SourceLoadError(
                "Impodo could not read the frozen generated table"
            ) from error


def project_lookup_values(
    factory: DuckDbConnectionFactory,
    *,
    rule: LookupRule,
    link: DerivedDatasetLink,
    snapshot: SourceSnapshot,
    path: Path,
    column_keys: Sequence[str],
    maximum_values: int | None,
) -> pl.DataFrame:
    """Project generated values or bounded SQL counts without physical lineage."""
    columns = {
        link.canonical_key_column_key: "canonical_key",
        link.name_column_key: "name",
        link.parent_key_column_key: "parent_key",
        link.company_column_key: "company",
    }
    if (
        not column_keys
        or len(set(column_keys)) != len(column_keys)
        or any(key not in columns for key in column_keys)
    ):
        raise SourceLoadError("Choose current generated-table columns")
    if maximum_values is not None and (len(column_keys) != 1 or maximum_values < 1):
        raise SourceLoadError("Choose a positive generated-value limit and one column")
    with _lookup_connection(
        factory, rule=rule, snapshot=snapshot, path=path, batch_size=1000
    ) as connection:
        if maximum_values is not None:
            rows = connection.execute(
                f"SELECT trim({columns[column_keys[0]]}, ?) AS value, COUNT(*) FROM lookup_records "
                "WHERE value IS NOT NULL AND value != '' GROUP BY value ORDER BY value LIMIT ?",
                [_CHOICE_WHITESPACE, maximum_values + 1],
            ).fetchall()
            return pl.DataFrame(
                rows,
                schema={column_keys[0]: pl.String, "count": pl.Int64},
                orient="row",
            )
        rows = connection.execute(
            f"SELECT {', '.join(columns[key] for key in column_keys)} FROM lookup_records ORDER BY generated_row"
        ).fetchall()
        return pl.DataFrame(
            rows, schema={key: pl.String for key in column_keys}, orient="row"
        )


def iter_lookup_batches(
    factory: DuckDbConnectionFactory,
    *,
    rule: LookupRule,
    link: DerivedDatasetLink,
    effective_dataset: SourceDataset,
    snapshot: SourceSnapshot,
    path: Path,
    batch_size: int,
) -> Iterator[GroupedExactJoinBatch]:
    """Stream the same generated records with complete ordered source membership."""
    with _lookup_connection(
        factory, rule=rule, snapshot=snapshot, path=path, batch_size=batch_size
    ) as connection:
        predicate = " AND ".join(
            f"raw.kind_{index} = input.kind_{index} AND raw.value_{index} IS NOT DISTINCT FROM input.value_{index}"
            for index in range(len(_source_keys(rule)))
        )
        cursor = connection.execute(f"""
            SELECT records.generated_row, records.canonical_key, records.name,
                   records.parent_key, records.company, records.aliases, records.first_source_row,
                   list(DISTINCT input.source_row ORDER BY input.source_row)
              FROM lookup_input AS input JOIN lookup_distinct AS raw ON {predicate}
              JOIN lookup_normalized AS normalized USING (raw_id)
              JOIN lookup_records AS records ON records.canonical_parts = normalized.canonical_parts
             GROUP BY ALL ORDER BY records.generated_row
        """)
        while batch := cursor.fetchmany(batch_size):
            if sum(len(item[7]) * 8 for item in batch) > _LOOKUP_LINEAGE_BATCH_BYTES:
                raise SourceLoadError(
                    "The generated table exceeds its bounded lineage transport budget"
                )
            rows, lineages, findings = [], [], []
            for (
                number,
                key,
                name,
                parent,
                company,
                aliases,
                first_row,
                members,
            ) in batch:
                values = {
                    link.canonical_key_column_key: key,
                    link.name_column_key: name,
                }
                if link.parent_key_column_key is not None:
                    values[link.parent_key_column_key] = parent
                if link.company_column_key is not None:
                    values[link.company_column_key] = company
                rows.append(
                    SourceRow(
                        int(number),
                        {
                            item.source_name: values[item.stable_key]
                            for item in effective_dataset.columns
                        },
                    )
                )
                lineages.append(
                    {snapshot.dataset_id: tuple(int(member) for member in members)}
                )
                findings.append(
                    (
                        Issue(
                            code="DERIVED_ALIAS_REVIEW_REQUIRED",
                            message=(
                                "multiple source spellings produce the same related record; review the preferred "
                                f"display value (first seen at source row {first_row})"
                            ),
                            severity=Severity.ERROR,
                            dataset=effective_dataset.name,
                            row=int(number),
                            field=link.name_column_key,
                        ),
                    )
                    if int(aliases) > 1
                    else ()
                )
            yield GroupedExactJoinBatch(tuple(rows), tuple(lineages), tuple(findings))
