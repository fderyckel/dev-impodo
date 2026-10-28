"""Evaluate grouped exact joins with bounded, set-based DuckDB scans.

Each verified Parquet input is scanned once into a narrow temporary key table.
All reconciliation, duplicate detection, matching, and lineage projection then
operate on those narrow tables. The connection is local to one synchronous
operation and is never cached across requests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import duckdb

from impodo.application.workspace.derived_entities import (
    ExactJoinGroupEvaluator,
    ExactJoinGroupPreview,
    ExactJoinGroupRequest,
)
from impodo.domain.serialization import CanonicalJsonObjectHasher, content_hash
from impodo.domain.source_snapshot import SourceCellKind, SourceSnapshot
from impodo.domain.structural import (
    ExactJoinExample,
    ExactJoinLineageReference,
    ExactJoinReview,
    ExactJoinRule,
    JoinKind,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.errors import WorkspaceError

from .database import DuckDbWorkspaceDatabase

_EVALUATION_MEMORY_LIMIT = "256MB"
_EVALUATION_THREADS = "2"
_LINEAGE_BATCH_ROWS = 2_000


@dataclass(frozen=True, slots=True)
class _InputStats:
    rows: int
    blank_rows: int
    invalid_rows: int
    duplicate_groups: int
    duplicate_rows: int


@dataclass(slots=True)
class _SqlSession:
    connection: duckdb.DuckDBPyConnection
    statement_count: int = 0

    def execute(
        self,
        query: str,
        parameters: list[object] | None = None,
    ) -> duckdb.DuckDBPyConnection:
        self.statement_count += 1
        return self.connection.execute(query, parameters or [])


class DuckDbExactJoinGroupEvaluator(ExactJoinGroupEvaluator):
    """Review grouped exact joins without materializing wide Python tables."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        self._connection_factory = database.connection_factory

    def review(
        self,
        *,
        request: ExactJoinGroupRequest,
        rules: tuple[ExactJoinRule, ...],
        selection: SourceSelection,
        snapshots: Mapping[str, SourceSnapshot],
        paths: Mapping[str, Path],
    ) -> ExactJoinGroupPreview:
        """Scan each participating snapshot once and return complete evidence."""

        participant_ids = (
            request.primary_dataset_id,
            *(item.dataset_id for item in request.additions),
        )
        if set(paths) != set(participant_ids) or not set(participant_ids).issubset(
            snapshots
        ):
            raise WorkspaceError("Grouped exact-join snapshot evidence is incomplete")
        datasets = {item.dataset_id: item for item in selection.datasets}
        if not set(participant_ids).issubset(datasets):
            raise WorkspaceError("Grouped exact-join source evidence is incomplete")

        with self._connection_factory.connect(
            Path(":memory:"),
            memory_limit=_EVALUATION_MEMORY_LIMIT,
            threads=_EVALUATION_THREADS,
            preserve_insertion_order=False,
            enable_external_access=True,
        ) as connection:
            sql = _SqlSession(connection)
            table_names: dict[str, str] = {}
            key_columns = {
                request.primary_dataset_id: request.primary_key_column_keys,
                **{
                    addition.dataset_id: addition.key_column_keys
                    for addition in request.additions
                },
            }
            for index, dataset_id in enumerate(participant_ids):
                table_name = f"_impodo_join_input_{index}"
                _create_key_table(
                    sql,
                    table_name=table_name,
                    path=paths[dataset_id],
                    snapshot=snapshots[dataset_id],
                    column_keys=key_columns[dataset_id],
                    value_types=request.key_value_types,
                )
                table_names[dataset_id] = table_name

            stats = {
                dataset_id: _input_stats(
                    sql,
                    table_names[dataset_id],
                    len(request.key_value_types),
                )
                for dataset_id in participant_ids
            }
            if any(
                stats[dataset_id].rows != snapshots[dataset_id].row_count
                for dataset_id in participant_ids
            ):
                raise WorkspaceError("Grouped exact-join snapshot row count changed")
            reviews = tuple(
                _review_addition(
                    sql,
                    primary_table=table_names[request.primary_dataset_id],
                    added_table=table_names[addition.dataset_id],
                    primary_stats=stats[request.primary_dataset_id],
                    added_stats=stats[addition.dataset_id],
                    key_count=len(request.key_value_types),
                    rule=rule,
                )
                for addition, rule in zip(request.additions, rules, strict=True)
            )
            if not all(review.can_save for review in reviews):
                return ExactJoinGroupPreview(
                    rules,
                    reviews,
                    physical_snapshot_scans=len(participant_ids),
                    statement_count=sql.statement_count,
                    bounded_execution_plan_verified=True,
                )

            complete = _complete_reviews(
                sql,
                request=request,
                rules=rules,
                reviews=reviews,
                selection=selection,
                table_names=table_names,
            )
            return ExactJoinGroupPreview(
                rules,
                complete,
                physical_snapshot_scans=len(participant_ids),
                statement_count=sql.statement_count,
                bounded_execution_plan_verified=True,
            )


def _create_key_table(
    sql: _SqlSession,
    *,
    table_name: str,
    path: Path,
    snapshot: SourceSnapshot,
    column_keys: tuple[str, ...],
    value_types: tuple[str, ...],
) -> None:
    columns = {item.stable_key: item for item in snapshot.schema.columns}
    if any(key not in columns for key in column_keys):
        raise WorkspaceError("Grouped exact-join snapshot fields changed")
    source_values: list[str] = []
    parsed_values: list[str] = []
    blank_parts: list[str] = []
    invalid_parts: list[str] = []
    for index, (column_key, value_type) in enumerate(
        zip(column_keys, value_types, strict=True),
        start=1,
    ):
        column = columns[column_key]
        value_alias = f"value_{index}"
        kind_alias = f"kind_{index}"
        source_values.extend(
            (
                f"{_identifier(column.value_column)} AS {value_alias}",
                f"{_identifier(column.kind_column)} AS {kind_alias}",
            )
        )
        blank = (
            f"({value_alias} IS NULL OR "
            f"({kind_alias} = {int(SourceCellKind.STRING)} "
            f"AND length(trim({value_alias})) = 0))"
        )
        parsed = _parsed_key_expression(value_alias, value_type)
        blank_parts.append(blank)
        parsed_values.append(f"{parsed} AS key_{index}")
        invalid_parts.append(f"key_{index} IS NULL")
    blank_expression = " OR ".join(blank_parts)
    invalid_expression = " OR ".join(invalid_parts)
    query = f"""
        CREATE TEMP TABLE {table_name} AS
        WITH source AS (
            SELECT {_identifier(snapshot.schema.source_row_column)} AS source_row,
                   {", ".join(source_values)}
              FROM read_parquet(?)
        ),
        parsed AS (
            SELECT source_row,
                   ({blank_expression}) AS blank_key,
                   {", ".join(parsed_values)}
              FROM source
        )
        SELECT source_row,
               blank_key,
               (NOT blank_key AND ({invalid_expression})) AS invalid_key,
               {", ".join(f"key_{index}" for index in range(1, len(column_keys) + 1))}
          FROM parsed
    """
    try:
        sql.execute(query, [str(path.resolve())])
    except duckdb.Error as error:
        raise WorkspaceError(
            "Impodo could not evaluate the accepted exact-join keys"
        ) from error
def _parsed_key_expression(value_alias: str, value_type: str) -> str:
    if value_type == "string":
        return value_alias
    if value_type == "integer":
        return (
            "CASE WHEN regexp_full_match("
            f"{value_alias}, '\\s*[+-]?[0-9]+\\s*') "
            f"THEN CAST(try_cast({value_alias} AS HUGEINT) AS VARCHAR) END"
        )
    if value_type == "decimal":
        return (
            f"CAST(try_cast(trim({value_alias}) AS DECIMAL(38, 18)) AS VARCHAR)"
        )
    if value_type == "boolean":
        return f"""
            CASE lower(trim({value_alias}))
              WHEN 'true' THEN 'true'
              WHEN '1' THEN 'true'
              WHEN 'yes' THEN 'true'
              WHEN 'y' THEN 'true'
              WHEN 'false' THEN 'false'
              WHEN '0' THEN 'false'
              WHEN 'no' THEN 'false'
              WHEN 'n' THEN 'false'
            END
        """
    if value_type == "date":
        return (
            f"CASE WHEN {value_alias} = trim({value_alias}) "
            f"THEN CAST(try_cast({value_alias} AS DATE) AS VARCHAR) END"
        )
    if value_type == "datetime":
        return f"""
            CASE WHEN {value_alias} = trim({value_alias}) THEN CAST(
              CASE
                WHEN regexp_matches({value_alias}, '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$')
                THEN epoch_us(try_cast({value_alias} AS TIMESTAMPTZ))
                ELSE epoch_us(try_cast({value_alias} AS TIMESTAMP))
              END AS VARCHAR
            ) END
        """
    raise WorkspaceError("Grouped exact-join key type is unsupported")


def _input_stats(
    sql: _SqlSession,
    table_name: str,
    key_count: int,
) -> _InputStats:
    keys = _key_columns(key_count)
    row = sql.execute(
        f"""
        WITH duplicates AS (
            SELECT COUNT(*) AS duplicate_rows
              FROM {table_name}
             WHERE NOT blank_key AND NOT invalid_key
             GROUP BY {keys}
            HAVING COUNT(*) > 1
        )
        SELECT (SELECT COUNT(*) FROM {table_name}),
               (SELECT COUNT(*) FROM {table_name} WHERE blank_key),
               (SELECT COUNT(*) FROM {table_name} WHERE invalid_key),
               (SELECT COUNT(*) FROM duplicates),
               (SELECT COALESCE(SUM(duplicate_rows), 0) FROM duplicates)
        """
    ).fetchone()
    if row is None:
        raise WorkspaceError("Grouped exact-join statistics are unavailable")
    return _InputStats(*(int(value) for value in row))


def _review_addition(
    sql: _SqlSession,
    *,
    primary_table: str,
    added_table: str,
    primary_stats: _InputStats,
    added_stats: _InputStats,
    key_count: int,
    rule: ExactJoinRule,
) -> ExactJoinReview:
    condition = _join_condition("left_input", "right_input", key_count)
    row = sql.execute(
        f"""
        SELECT COUNT(DISTINCT left_input.source_row),
               COUNT(DISTINCT right_input.source_row)
          FROM {primary_table} AS left_input
          JOIN {added_table} AS right_input ON {condition}
         WHERE NOT left_input.blank_key AND NOT left_input.invalid_key
           AND NOT right_input.blank_key AND NOT right_input.invalid_key
        """
    ).fetchone()
    if row is None:
        raise WorkspaceError("Grouped exact-join matches are unavailable")
    matched_left = int(row[0])
    matched_right = int(row[1])
    examples = _examples(
        sql,
        primary_table=primary_table,
        added_table=added_table,
        key_count=key_count,
    )
    unmatched_left = (
        primary_stats.rows
        - matched_left
        - primary_stats.blank_rows
        - primary_stats.invalid_rows
    )
    unmatched_right = (
        added_stats.rows
        - matched_right
        - added_stats.blank_rows
        - added_stats.invalid_rows
    )
    findings: list[str] = []
    if primary_stats.blank_rows:
        findings.append("Join key is blank on left-side rows")
    if primary_stats.invalid_rows:
        findings.append("Join key is invalid on left-side rows")
    if added_stats.blank_rows:
        findings.append("Join key is blank on right-side rows")
    if added_stats.invalid_rows:
        findings.append("Join key is invalid on right-side rows")
    if added_stats.duplicate_groups:
        findings.append("Join right-side keys are not unique")
    if rule.require_unique_left and primary_stats.duplicate_groups:
        findings.append("Join base keys are not unique")
    if rule.kind is JoinKind.INNER and unmatched_left:
        findings.append("Inner join has unmatched left rows")
    if rule.require_all_right_rows and unmatched_right:
        findings.append("Join has unrepresented right-side rows")
    return ExactJoinReview(
        left_rows=primary_stats.rows,
        right_rows=added_stats.rows,
        matched_left_rows=matched_left,
        unmatched_left_rows=unmatched_left,
        matched_right_rows=matched_right,
        unmatched_right_rows=unmatched_right,
        blank_left_key_rows=primary_stats.blank_rows,
        invalid_left_key_rows=primary_stats.invalid_rows,
        blank_right_key_rows=added_stats.blank_rows,
        invalid_right_key_rows=added_stats.invalid_rows,
        duplicate_left_key_groups=primary_stats.duplicate_groups,
        duplicate_left_key_rows=primary_stats.duplicate_rows,
        duplicate_right_key_groups=added_stats.duplicate_groups,
        duplicate_right_key_rows=added_stats.duplicate_rows,
        expected_output_rows=primary_stats.rows,
        actual_output_rows=(primary_stats.rows if not findings else None),
        findings=tuple(findings),
        examples=examples,
    )


def _examples(
    sql: _SqlSession,
    *,
    primary_table: str,
    added_table: str,
    key_count: int,
) -> tuple[ExactJoinExample, ...]:
    condition = _join_condition("left_input", "right_input", key_count)
    rows = sql.execute(
        f"""
        WITH matched AS (
            SELECT 0 AS category,
                   left_input.source_row AS left_row,
                   MIN(right_input.source_row) AS right_row
              FROM {primary_table} AS left_input
              JOIN {added_table} AS right_input ON {condition}
             WHERE NOT left_input.blank_key AND NOT left_input.invalid_key
               AND NOT right_input.blank_key AND NOT right_input.invalid_key
             GROUP BY left_input.source_row
             ORDER BY left_input.source_row
             LIMIT 5
        ),
        unmatched AS (
            SELECT 1 AS category,
                   left_input.source_row AS left_row,
                   NULL::BIGINT AS right_row
              FROM {primary_table} AS left_input
             WHERE NOT left_input.blank_key AND NOT left_input.invalid_key
               AND NOT EXISTS (
                   SELECT 1 FROM {added_table} AS right_input
                    WHERE NOT right_input.blank_key AND NOT right_input.invalid_key
                      AND {condition}
               )
             ORDER BY left_input.source_row
             LIMIT 5
        )
        SELECT left_row, right_row
          FROM (SELECT * FROM matched UNION ALL SELECT * FROM unmatched)
         ORDER BY category, left_row
        """
    ).fetchall()
    return tuple(
        ExactJoinExample(int(left), int(right) if right is not None else None)
        for left, right in rows
    )


def _complete_reviews(
    sql: _SqlSession,
    *,
    request: ExactJoinGroupRequest,
    rules: tuple[ExactJoinRule, ...],
    reviews: tuple[ExactJoinReview, ...],
    selection: SourceSelection,
    table_names: dict[str, str],
) -> tuple[ExactJoinReview, ...]:
    aliases = ["primary_input"] + [
        f"addition_{index}" for index in range(1, len(request.additions) + 1)
    ]
    primary_table = table_names[request.primary_dataset_id]
    joins = "\n".join(
        (
            f"LEFT JOIN {table_names[addition.dataset_id]} AS {aliases[index]} "
            f"ON {_join_condition(aliases[0], aliases[index], len(request.key_value_types))}"
        )
        for index, addition in enumerate(request.additions, start=1)
    )
    selected_rows = ", ".join(
        f"{alias}.source_row AS source_row_{index}"
        for index, alias in enumerate(aliases)
    )
    cursor = sql.execute(
        f"""
        SELECT * FROM (
            SELECT ROW_NUMBER() OVER (ORDER BY {aliases[0]}.source_row) AS output_row,
                   {selected_rows}
              FROM {primary_table} AS {aliases[0]}
              {joins}
             WHERE NOT {aliases[0]}.blank_key
               AND NOT {aliases[0]}.invalid_key
        ) AS joined_rows
        ORDER BY CAST(output_row AS VARCHAR)
        """
    )
    hashers = [CanonicalJsonObjectHasher() for _rule in rules]
    lineage_links = [0 for _rule in rules]
    example_rows = {example.left_row_number for review in reviews for example in review.examples}
    example_lineage: list[dict[int, tuple[ExactJoinLineageReference, ...]]] = [
        {} for _rule in rules
    ]
    dataset_ids = (
        request.primary_dataset_id,
        *(item.dataset_id for item in request.additions),
    )
    while batch := cursor.fetchmany(_LINEAGE_BATCH_ROWS):
        for raw in batch:
            output_row = int(raw[0])
            source_rows = tuple(
                int(value) if value is not None else None for value in raw[1:]
            )
            for step in range(len(rules)):
                sources = {
                    dataset_id: [source_row]
                    for dataset_id, source_row in zip(
                        dataset_ids[: step + 2],
                        source_rows[: step + 2],
                        strict=True,
                    )
                    if source_row is not None
                }
                hashers[step].add_value(str(output_row), sources)
                lineage_links[step] += len(sources)
                primary_row = source_rows[0]
                if primary_row in example_rows:
                    example_lineage[step][primary_row] = tuple(
                        ExactJoinLineageReference(dataset_id, (source_row,))
                        for dataset_id, source_row in sorted(
                            (
                                (dataset_id, source_row)
                                for dataset_id, source_row in zip(
                                    dataset_ids[: step + 2],
                                    source_rows[: step + 2],
                                    strict=True,
                                )
                                if source_row is not None
                            )
                        )
                    )

    output_hashes = _output_table_hashes(selection, rules)
    return tuple(
        replace(
            review,
            actual_output_rows=review.expected_output_rows,
            lineage_links=lineage_links[index],
            output_table_hash=output_hashes[index],
            lineage_hash=hashers[index].finish(),
            examples=tuple(
                replace(
                    example,
                    physical_lineage=example_lineage[index][example.left_row_number],
                )
                for example in review.examples
            ),
        )
        for index, review in enumerate(reviews)
    )


def _output_table_hashes(
    selection: SourceSelection,
    rules: tuple[ExactJoinRule, ...],
) -> tuple[str, ...]:
    available = {
        dataset.dataset_id: dataset.source_evidence_hash
        for dataset in selection.datasets
    }
    result: list[str] = []
    for rule in rules:
        output_hash = content_hash(
            {
                "rule": rule.to_dict(),
                "sources": {
                    rule.left_dataset_id: available[rule.left_dataset_id],
                    rule.right_dataset_id: available[rule.right_dataset_id],
                },
            }
        )
        available[structural_dataset_id(rule)] = output_hash
        result.append(output_hash)
    return tuple(result)


def _join_condition(left: str, right: str, key_count: int) -> str:
    return " AND ".join(
        f"{left}.key_{index} = {right}.key_{index}"
        for index in range(1, key_count + 1)
    )


def _key_columns(key_count: int) -> str:
    return ", ".join(f"key_{index}" for index in range(1, key_count + 1))


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
