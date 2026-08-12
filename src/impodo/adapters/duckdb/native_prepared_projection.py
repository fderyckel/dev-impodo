"""Set-based projection of clean native prepared snapshots into Stage E facts.

This module deliberately owns the DuckDB SQL boundary instead of growing the
preparation-session repository.  It projects already-transformed Parquet
columns into the narrow canonical index, lineage, identity, relationship, and
impact relations without constructing Python objects per source row.

Rows containing native transformation issues are not accepted here.  The
caller must route the complete dataset through the bounded semantic projector;
there is never a per-row fallback inside this plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from ..polars_transformation import PREPARED_ORDINAL_COLUMN

from ...domain.compiler.columnar_transformation import (
    ColumnarIdentityComponentProgram,
    ColumnarOperationKind,
    ColumnarTransformationProgram,
)
from ...domain.source_snapshot import (
    SOURCE_ROW_COLUMN,
    SourceCellKind,
    source_kind_column,
    source_value_column,
)
from ...domain.staging.preparation_session import PreparedCanonicalProjection
from ...domain.staging.transformation_impact import TransformationImpactCounts
from ...workspace_errors import WorkspaceError


_ISSUE_COLUMN = "__impodo_columnar_issues"


@dataclass(frozen=True, slots=True)
class NativePreparedProjectionResult:
    """Manifest-sized evidence returned by one set-based dataset projection."""

    row_count: int
    impact_counts: TransformationImpactCounts
    control_totals: tuple["NativeControlTotalValue", ...]
    scan_count: int
    statement_count: int
    optimized_plan_verified: bool
    bounded_execution_plan_verified: bool


@dataclass(frozen=True, slots=True)
class NativeControlTotalValue:
    """One exact set-based accumulator result for a declared control field."""

    target_field: str
    actual_total: str
    included_rows: int
    empty_rows: int


@dataclass(frozen=True, slots=True)
class _ValueColumn:
    alias: str
    value_type: str


@dataclass(frozen=True, slots=True)
class _IdentityComponent:
    program: ColumnarIdentityComponentProgram
    values: tuple[_ValueColumn, ...]


@dataclass(frozen=True, slots=True)
class _ProjectionLayout:
    scalars: tuple[_ValueColumn, ...]
    source_identity: tuple[_IdentityComponent, ...]
    target_identity: tuple[_IdentityComponent, ...]
    target_scope: tuple[_IdentityComponent, ...]
    relationships: tuple[_IdentityComponent, ...]


def supports_clean_native_projection(
    connection: duckdb.DuckDBPyConnection,
    path: str | Path,
    program: ColumnarTransformationProgram,
    control_fields: tuple[str, ...] = (),
) -> bool:
    """Return whether one entire prepared dataset can use the clean SQL plan."""

    layout = _layout(program)
    supported_types = {"string", "integer", "decimal"}
    if any(value.value_type not in supported_types for value in _all_values(layout)):
        return False
    row = connection.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE len({_identifier(_ISSUE_COLUMN)}) > 0),
               COUNT(*) FILTER (WHERE NOT (
                   {_integer_compatibility(layout)}
                   AND {_source_display_compatibility(program)}
               ))
          FROM read_parquet(?)
        """,
        [str(Path(path).resolve())],
    ).fetchone()
    return (
        row is not None
        and int(row[0]) == 0
        and int(row[1]) == 0
        and _control_totals(connection, path, program, layout, control_fields)
        is not None
    )


def append_clean_native_projection(
    connection: duckdb.DuckDBPyConnection,
    *,
    path: str | Path,
    session_id: str,
    projection: PreparedCanonicalProjection,
    control_fields: tuple[str, ...] = (),
) -> NativePreparedProjectionResult:
    """Insert one issue-free native dataset using bounded set-based statements."""

    if not projection.set_based_projection:
        raise ValueError("Native projection metadata must select the set-based route")
    program = projection.program
    layout = _layout(program)
    source_path = str(Path(path).resolve())
    control_totals = _control_totals(
        connection,
        source_path,
        program,
        layout,
        control_fields,
    )
    assert control_totals is not None

    relation = _projected_relation_sql(
        projection,
        layout,
        include_canonical_values=False,
    )
    params = [source_path]
    ordinal = f"{projection.ordinal_start} + {_identifier(PREPARED_ORDINAL_COLUMN)}"
    base_disposition = "REFERENCE" if projection.mode == "reference" else "CANDIDATE"

    connection.execute(
        f"""
        INSERT INTO canonical_staging_row (
            run_id, ordinal, row_id, dataset, source_row, target_model,
            disposition, record_label, quality_identity_key, row_json
        )
        SELECT {_literal(session_id)}, {ordinal}, row_id,
               {_literal(projection.dataset)}, {_identifier(SOURCE_ROW_COLUMN)},
               {_literal(program.target_model)}, {_literal(base_disposition)},
               record_label, quality_identity_key, ''
          FROM ({relation}) AS projected
         ORDER BY {_identifier(SOURCE_ROW_COLUMN)}
        """,
        params,
    )
    connection.execute(
        f"""
        INSERT INTO preparation_direct_identity (
            session_id, ordinal, dataset, identity_hash,
            base_disposition, finalized_duplicate
        )
        SELECT {_literal(session_id)}, {ordinal}, {_literal(projection.dataset)},
               identity_hash, {_literal(base_disposition)}, FALSE
          FROM ({relation}) AS projected
         ORDER BY {_identifier(SOURCE_ROW_COLUMN)}
        """,
        params,
    )

    relationship_items = _relationship_items_sql(projection, layout)
    if relationship_items:
        relationship_ordinal = (
            f"{projection.ordinal_start} + {_identifier(PREPARED_ORDINAL_COLUMN)}"
        )
        connection.execute(
            f"""
            INSERT INTO preparation_relationship_edge (
                session_id, child_ordinal, target_field, item_ordinal,
                parent_dataset, normalized_key_json, parent_identity_hash,
                match_state, resolution_state, match_count,
                resolved_parent_ordinal
            )
            SELECT {_literal(session_id)}, {relationship_ordinal},
                   item.target_field, 0,
                   item.parent_dataset, CAST(item.key_json AS VARCHAR),
                   'sha256:' || sha256(CAST(json_object(
                       'dataset', item.parent_dataset,
                       'source_identity', item.key_json
                   ) AS VARCHAR)),
                   'PENDING', 'PENDING', 0, NULL
              FROM read_parquet(?),
                   UNNEST([{relationship_items}]) AS edge(item)
             WHERE item.key_json IS NOT NULL
             ORDER BY {_identifier(SOURCE_ROW_COLUMN)}, item.field_order
            """,
            params,
        )

    connection.execute(
        f"""
        INSERT INTO preparation_lineage
        SELECT {_literal(session_id)}, {_literal(projection.dataset)},
               {_identifier(SOURCE_ROW_COLUMN)},
               {_literal(projection.physical_dataset_id)},
               {_identifier(SOURCE_ROW_COLUMN)}
          FROM read_parquet(?)
         ORDER BY {_identifier(SOURCE_ROW_COLUMN)}
        """,
        params,
    )
    connection.execute(
        f"""
        INSERT OR IGNORE INTO preparation_physical_row
        SELECT {_literal(session_id)},
               {_literal(projection.physical_dataset_id)},
               {_identifier(SOURCE_ROW_COLUMN)}
          FROM read_parquet(?)
         ORDER BY {_identifier(SOURCE_ROW_COLUMN)}
        """,
        params,
    )

    impact_items = _impact_items_sql(projection, layout)
    impact_relation = _sparse_impact_relation_sql(impact_items)
    counts_row = connection.execute(
        f"""
        SELECT SUM(len(items)),
               SUM(len(list_filter(items, item -> item.outcome = 'changed'))),
               SUM(len(list_filter(items, item -> item.outcome = 'fallback'))),
               SUM(len(list_filter(items, item -> item.outcome = 'null'))),
               SUM(len(list_filter(items, item -> item.outcome = 'invalid'))),
               SUM(len(list_filter(items, item -> item.outcome = 'provided'))),
               SUM(len(list_filter(items, item -> item.outcome = 'unchanged')))
          FROM (
                SELECT [{impact_items}] AS items
                  FROM read_parquet(?)
          ) AS impact_counts
        """,
        params,
    ).fetchone()
    if counts_row is None:
        raise WorkspaceError("Native impact accounting is missing")
    counts = TransformationImpactCounts(
        evaluated_count=int(counts_row[0]),
        changed_count=int(counts_row[1]),
        fallback_count=int(counts_row[2]),
        null_count=int(counts_row[3]),
        invalid_count=int(counts_row[4]),
        provided_count=int(counts_row[5]),
        unchanged_count=int(counts_row[6]),
    )
    impact_start_row = connection.execute(
        "SELECT impact_row_count FROM preparation_session WHERE session_id = ?",
        [session_id],
    ).fetchone()
    if impact_start_row is None:
        raise WorkspaceError("Preparation session was not found")
    impact_start = int(impact_start_row[0])
    connection.execute(
        f"""
        INSERT INTO preparation_impact_row (
            session_id, ordinal, dataset, source_row,
            target_field, outcome, impact_json
        )
        SELECT {_literal(session_id)},
               {impact_start} + ROW_NUMBER() OVER (
                   ORDER BY source_row, impact_order
               ) - 1,
               dataset, source_row, target_field, outcome,
               CAST(json_object(
                   'dataset', dataset,
                   'message', message,
                   'outcome', outcome,
                   'proposed_value', proposed_value,
                   'raw_value', raw_value,
                   'rules', rules,
                   'source_column', source_column,
                   'source_row', source_row,
                   'target_field', target_field
               ) AS VARCHAR)
          FROM ({impact_relation}) AS impacts
         WHERE outcome <> 'unchanged'
         ORDER BY source_row, impact_order
        """,
        params,
    )
    if counts.impact_count:
        connection.execute(
            """
            UPDATE preparation_session
               SET impact_row_count = impact_row_count + ?
             WHERE session_id = ?
            """,
            [counts.impact_count, session_id],
        )

    connection.execute(
        """
        UPDATE preparation_session
           SET staged_row_count = staged_row_count + ?
         WHERE session_id = ?
        """,
        [projection.row_count, session_id],
    )
    scan_count = 7 + (4 * len(control_fields)) + int(bool(relationship_items))
    optimized_plan_verified = _verify_projector_plan(
        connection,
        source_path,
        projection,
    )
    statement_count = (
        11
        + (4 * len(control_fields))
        + int(bool(relationship_items))
        + int(bool(counts.impact_count))
    )
    return NativePreparedProjectionResult(
        row_count=projection.row_count,
        impact_counts=counts,
        control_totals=control_totals,
        scan_count=scan_count,
        statement_count=statement_count,
        optimized_plan_verified=optimized_plan_verified,
        bounded_execution_plan_verified=optimized_plan_verified,
    )


def projected_encoded_rows_sql(
    projection: PreparedCanonicalProjection,
) -> str:
    """Return a bounded projector query with one Parquet parameter."""

    if not projection.set_based_projection:
        raise ValueError("The SQL projector requires a set-based projection")
    layout = _layout(projection.program)
    relation = _projected_relation_sql(
        projection,
        layout,
        prepared_ordinal_filter=True,
    )
    source_row = _identifier(SOURCE_ROW_COLUMN)
    ordinal = f"{projection.ordinal_start} + {_identifier(PREPARED_ORDINAL_COLUMN)}"
    base_disposition = "REFERENCE" if projection.mode == "reference" else "CANDIDATE"
    disposition = _literal(base_disposition)
    issues = "CAST('[]' AS JSON)"
    select_metadata = (
        f"{ordinal}, row_id, {_literal(projection.dataset)}, {source_row}, "
        f"{_literal(projection.program.target_model)}, {_literal(base_disposition)}"
    )
    row_json = _canonical_row_json(
        projection,
        source_identity="source_identity_json",
        target_identity="target_identity_json",
        target_scope="target_scope_json",
        proposed_values="proposed_values_json",
        references="references_json",
        lineage="lineage_json",
        row_id="row_id",
        disposition=disposition,
        issues=issues,
    )
    return f"""
        SELECT {select_metadata}, CAST({row_json} AS VARCHAR)
          FROM ({relation}) AS projected
         ORDER BY {source_row}
    """


def _verify_projector_plan(
    connection: duckdb.DuckDBPyConnection,
    path: str | Path,
    projection: PreparedCanonicalProjection,
) -> bool:
    """Inspect a sanitized page plan for pushdown and forbidden fallbacks."""

    page_stop = min(projection.row_count, 1_000)
    rows = connection.execute(
        "EXPLAIN " + projected_encoded_rows_sql(projection),
        [str(Path(path).resolve()), 0, page_stop],
    ).fetchall()
    plan = "\n".join(str(value) for row in rows for value in row)
    upper = plan.upper()
    parquet_scan = "READ_PARQUET" in upper or "PARQUET_SCAN" in upper
    return (
        parquet_scan
        and PREPARED_ORDINAL_COLUMN in plan
        and "FILTER" in upper
        and "PYTHON_SCAN" not in upper
        and "WINDOW" not in upper
    )


def _projected_relation_sql(
    projection: PreparedCanonicalProjection,
    layout: _ProjectionLayout,
    *,
    prepared_ordinal_filter: bool = False,
    include_canonical_values: bool = True,
) -> str:
    program = projection.program
    source_identity = _identity_json(layout.source_identity)
    target_identity = _identity_json(layout.target_identity)
    target_scope = _identity_json(layout.target_scope)
    proposed = _object_json(
        tuple(
            (field.target_field, _portable_json(value.alias, value.value_type))
            for field, value in zip(program.scalar_fields, layout.scalars, strict=True)
        )
    )
    references = _references_json(projection, layout)
    lineage = _lineage_json(projection)
    row_id_input = (
        "json_object('lineage', lineage_json, "
        "'source_identity', source_identity_json, "
        f"'target_model', {_literal(program.target_model)})"
    )
    identity_input = (
        f"json_object('dataset', {_literal(projection.dataset)}, "
        "'source_identity', source_identity_json)"
    )
    quality_input = (
        f"json_object('dataset', {_literal(projection.dataset)}, "
        "'identity', target_identity_json, "
        f"'model', {_literal(program.target_model)}, 'scope', target_scope_json)"
    )
    record_values = [
        _display_sql(value.alias, value.value_type)
        for value in (
            *_flatten(layout.target_identity),
            *_flatten(layout.source_identity),
        )
    ]
    record_list = ", ".join(record_values) or "NULL"
    record_label = f"""
        CASE WHEN len(list_filter([{record_list}], value ->
                 value IS NOT NULL AND value <> '')) = 0
             THEN 'Row ' || CAST({_identifier(SOURCE_ROW_COLUMN)} AS VARCHAR)
             ELSE substr(array_to_string(list_slice(list_filter(
                 [{record_list}], value -> value IS NOT NULL AND value <> ''
             ), 1, 2), ' / '), 1, 120)
        END
    """
    quality_complete = (
        " AND ".join(
            f"{value.alias} IS NOT NULL AND {_display_sql(value.alias, value.value_type)} <> ''"
            for value in (
                *_flatten(layout.target_identity),
                *_flatten(layout.target_scope),
            )
        )
        or "FALSE"
    )
    source_filter = (
        f"WHERE {_identifier(PREPARED_ORDINAL_COLUMN)} >= ? "
        f"AND {_identifier(PREPARED_ORDINAL_COLUMN)} < ?"
        if prepared_ordinal_filter
        else ""
    )
    canonical_value_columns = (
        f"{proposed} AS proposed_values_json, {references} AS references_json,"
        if include_canonical_values
        else ""
    )
    return f"""
        WITH values AS (
            SELECT *,
                   {source_identity} AS source_identity_json,
                   {target_identity} AS target_identity_json,
                   {target_scope} AS target_scope_json,
                   {canonical_value_columns}
                   {lineage} AS lineage_json
              FROM read_parquet(?)
             {source_filter}
        )
        SELECT *,
               'sha256:' || sha256(CAST({row_id_input} AS VARCHAR)) AS row_id,
               'sha256:' || sha256(CAST({identity_input} AS VARCHAR))
                   AS identity_hash,
               CASE WHEN {quality_complete}
                    THEN 'sha256:' || sha256(CAST({quality_input} AS VARCHAR))
                    ELSE NULL END AS quality_identity_key,
               {record_label} AS record_label
          FROM values
    """


def _canonical_row_json(
    projection: PreparedCanonicalProjection,
    *,
    source_identity: str,
    target_identity: str,
    target_scope: str,
    proposed_values: str,
    references: str,
    lineage: str,
    row_id: str,
    disposition: str,
    issues: str,
) -> str:
    return f"""
        json_object(
            'dataset', {_literal(projection.dataset)},
            'disposition', {disposition},
            'issues', {issues},
            'lineage', {lineage},
            'proposed_values', {proposed_values},
            'references', {references},
            'row_id', {row_id},
            'source_identity', {source_identity},
            'source_row', {_identifier(SOURCE_ROW_COLUMN)},
            'target_identity', {target_identity},
            'target_model', {_literal(projection.program.target_model)},
            'target_scope', {target_scope}
        )
    """


def _lineage_json(projection: PreparedCanonicalProjection) -> str:
    field_sources = _object_json(
        tuple(
            (field, _string_array_json(sources))
            for field, sources in sorted(projection.field_sources.items())
        )
    )
    source_row = _identifier(SOURCE_ROW_COLUMN)
    return f"""
        json_object(
            'dataset', {_literal(projection.dataset)},
            'derived_plan_hash', NULL,
            'field_sources', {field_sources},
            'mapping_hash', {_literal(projection.program.mapping_content_hash)},
            'physical_dataset_id', {_literal(projection.physical_dataset_id)},
            'physical_source_rows', json_array({source_row}),
            'schema_hash', {_literal(projection.program.schema_hash)},
            'source_hash', {_literal(projection.source_hash)},
            'source_row', {source_row},
            'source_selection_hash', {_literal(projection.program.source_selection_hash)}
        )
    """


def _references_json(
    projection: PreparedCanonicalProjection,
    layout: _ProjectionLayout,
) -> str:
    fields = []
    for relationship, component in zip(
        projection.program.relationships,
        layout.relationships,
        strict=True,
    ):
        key = _identity_json((component,))
        all_null = " AND ".join(f"{item.alias} IS NULL" for item in component.values)
        value = f"""
            CASE WHEN {all_null} THEN NULL ELSE json_object(
                'dataset', {_literal(relationship.parent_dataset_name)},
                'key', {key},
                'origin', 'incoming',
                'scope', json_array()
            ) END
        """
        fields.append((relationship.target_field, value))
    return _object_json(tuple(fields))


def _relationship_items_sql(
    projection: PreparedCanonicalProjection,
    layout: _ProjectionLayout,
) -> str:
    items = []
    for index, (relationship, component) in enumerate(
        zip(projection.program.relationships, layout.relationships, strict=True)
    ):
        all_null = " AND ".join(f"{item.alias} IS NULL" for item in component.values)
        key = _identity_json((component,))
        items.append(
            "struct_pack("
            f"field_order := {index}, "
            f"target_field := {_literal(relationship.target_field)}, "
            f"parent_dataset := {_literal(relationship.parent_dataset_name)}, "
            f"key_json := CASE WHEN {all_null} THEN NULL ELSE {key} END)"
        )
    return ", ".join(items)


def _impact_items_sql(
    projection: PreparedCanonicalProjection,
    layout: _ProjectionLayout,
) -> str:
    program = projection.program
    items: list[str] = []
    order = 0
    for component in (*layout.target_identity, *layout.target_scope):
        raw = _joined_display(
            tuple(
                _source_display_sql(item.ordinal)
                for item in component.program.source_columns
            )
        )
        proposed = _joined_display(
            tuple(
                _display_sql(item.alias, item.value_type) for item in component.values
            )
        )
        for target_field in component.program.target_fields:
            items.append(
                _impact_struct(
                    order=order,
                    dataset=projection.dataset,
                    source_column=component.program.source_label,
                    target_field=target_field,
                    raw=raw,
                    proposed=proposed,
                    rules="Identity preparation",
                    outcome=f"CASE WHEN {raw} = {proposed} THEN 'unchanged' ELSE 'changed' END",
                )
            )
            order += 1
    for field, value in zip(program.scalar_fields, layout.scalars, strict=True):
        raw = (
            _source_display_sql(field.provider.source.ordinal)
            if field.provider.source is not None
            else _literal("—")
        )
        proposed = _display_sql(value.alias, value.value_type)
        if field.provider.operation is ColumnarOperationKind.USE_CONSTANT:
            outcome = "'provided'"
        else:
            fallback = (
                f"WHEN {_identifier(_scalar_fallback_alias(program.scalar_fields.index(field)))} "
                "THEN 'fallback' "
                if field.provider.operation is ColumnarOperationKind.SOURCE_FALLBACK
                else ""
            )
            raw_kind = (
                _identifier(source_kind_column(field.provider.source.ordinal))
                if field.provider.source is not None
                else str(int(SourceCellKind.NULL))
            )
            outcome = (
                "CASE "
                + fallback
                + f"WHEN {value.alias} IS NULL AND {raw_kind} <> {int(SourceCellKind.NULL)} "
                "THEN 'null' "
                + f"WHEN {raw} <> {proposed} THEN 'changed' ELSE 'unchanged' END"
            )
        items.append(
            _impact_struct(
                order=order,
                dataset=projection.dataset,
                source_column=field.source_label,
                target_field=field.target_field,
                raw=raw,
                proposed=proposed,
                rules=field.transformation_rules,
                outcome=outcome,
            )
        )
        order += 1
    if not items:
        raise WorkspaceError("Native transformation program has no impact facts")
    return ", ".join(items)


def _sparse_impact_relation_sql(items: str) -> str:
    return f"""
        SELECT item.*
          FROM read_parquet(?),
               UNNEST(list_filter(
                   [{items}], item -> item.outcome <> 'unchanged'
               )) AS facts(item)
    """


def _impact_struct(
    *,
    order: int,
    dataset: str,
    source_column: str,
    target_field: str,
    raw: str,
    proposed: str,
    rules: str,
    outcome: str,
) -> str:
    return (
        "struct_pack("
        f"impact_order := {order}, dataset := {_literal(dataset)}, "
        f"source_row := {_identifier(SOURCE_ROW_COLUMN)}, "
        f"source_column := {_literal(source_column)}, "
        f"target_field := {_literal(target_field)}, raw_value := {raw}, "
        f"proposed_value := {proposed}, rules := {_literal(rules)}, "
        f"outcome := {outcome}, message := '')"
    )


def _layout(program: ColumnarTransformationProgram) -> _ProjectionLayout:
    scalars = tuple(
        _ValueColumn(
            alias=(
                _scalar_prepared_alias(index)
                if field.conversion_step.operation
                is ColumnarOperationKind.PARSE_INTEGER
                else _scalar_value_alias(index)
            ),
            value_type=field.value_type,
        )
        for index, field in enumerate(program.scalar_fields)
    )
    return _ProjectionLayout(
        scalars=scalars,
        source_identity=_identity_layout(program.source_identity, "source_identity"),
        target_identity=_identity_layout(program.target_identity, "target_identity"),
        target_scope=_identity_layout(program.target_scope, "target_scope"),
        relationships=_identity_layout(
            tuple(item.key for item in program.relationships),
            "relationship",
        ),
    )


def _identity_layout(
    components: tuple[ColumnarIdentityComponentProgram, ...],
    role: str,
) -> tuple[_IdentityComponent, ...]:
    result = []
    for component_index, component in enumerate(components):
        conversion = next(
            (
                step.operation
                for step in component.normalization_steps
                if step.operation.name.startswith("PARSE_")
            ),
            ColumnarOperationKind.PARSE_STRING,
        )
        values = tuple(
            _ValueColumn(
                alias=(
                    f"__impodo_{role}_{component_index:04d}_{source_index:04d}_normalized"
                    if conversion
                    in {
                        ColumnarOperationKind.PARSE_STRING,
                        ColumnarOperationKind.PARSE_INTEGER,
                    }
                    else f"__impodo_{role}_{component_index:04d}_{source_index:04d}_value"
                ),
                value_type=component.value_type,
            )
            for source_index, _source in enumerate(component.source_columns)
        )
        result.append(_IdentityComponent(program=component, values=values))
    return tuple(result)


def _portable_json(alias: str, value_type: str) -> str:
    column = _identifier(alias)
    if value_type == "decimal":
        value = _decimal_text(column)
        return f"CASE WHEN {column} IS NULL THEN NULL ELSE json_object('type', 'decimal', 'value', {value}) END"
    if value_type == "integer":
        return f"to_json(CAST({column} AS HUGEINT))"
    return f"to_json({column})"


def _display_sql(alias: str, value_type: str) -> str:
    column = _identifier(alias)
    if value_type == "decimal":
        rendered = _decimal_text(column)
    elif value_type == "integer":
        rendered = f"CAST(CAST({column} AS HUGEINT) AS VARCHAR)"
    else:
        rendered = f"CAST({column} AS VARCHAR)"
    return f"CASE WHEN {column} IS NULL THEN '—' ELSE {rendered} END"


def _source_display_sql(ordinal: int) -> str:
    value = _identifier(source_value_column(ordinal))
    kind = _identifier(source_kind_column(ordinal))
    return f"""
        CASE
            WHEN {kind} = {int(SourceCellKind.NULL)} THEN '—'
            WHEN {kind} = {int(SourceCellKind.DATETIME)} THEN
                CASE WHEN try_cast({value} AS TIMESTAMPTZ) IS NULL
                     THEN {value}
                     ELSE {_datetime_text(f"try_cast({value} AS TIMESTAMPTZ)")}
                END
            ELSE {value}
        END
    """


def _decimal_text(column: str) -> str:
    unsigned = f"ltrim({column}, '+-')"
    integer = f"split_part({unsigned}, '.', 1)"
    fraction = f"split_part({unsigned}, '.', 2)"
    normalized_integer = f"COALESCE(NULLIF(ltrim({integer}, '0'), ''), '0')"
    sign = f"CASE WHEN starts_with({column}, '-') THEN '-' ELSE '' END"
    return (
        f"{sign} || {normalized_integer} || CASE WHEN contains({unsigned}, '.') "
        f"THEN '.' || {fraction} ELSE '' END"
    )


def _datetime_text(column: str) -> str:
    utc = f"timezone('UTC', {column})"
    return f"""
        CASE WHEN date_part('microsecond', {utc}) % 1000000 = 0
             THEN strftime({utc}, '%Y-%m-%dT%H:%M:%SZ')
             ELSE strftime({utc}, '%Y-%m-%dT%H:%M:%S.%fZ')
        END
    """


def _identity_json(components: tuple[_IdentityComponent, ...]) -> str:
    values = tuple(
        _portable_json(item.alias, item.value_type)
        for component in components
        for item in component.values
    )
    return f"json_array({', '.join(values)})" if values else "json_array()"


def _object_json(fields: tuple[tuple[str, str], ...]) -> str:
    if not fields:
        return "json_object()"
    arguments = ", ".join(
        f"{_literal(name)}, {value}" for name, value in sorted(fields)
    )
    return f"json_object({arguments})"


def _string_array_json(values: tuple[str, ...]) -> str:
    return f"json_array({', '.join(_literal(value) for value in values)})"


def _joined_display(values: tuple[str, ...]) -> str:
    return " || ' | ' || ".join(f"({value})" for value in values) or "''"


def _flatten(components: tuple[_IdentityComponent, ...]) -> tuple[_ValueColumn, ...]:
    return tuple(item for component in components for item in component.values)


def _all_values(layout: _ProjectionLayout) -> tuple[_ValueColumn, ...]:
    return (
        *layout.scalars,
        *_flatten(layout.source_identity),
        *_flatten(layout.target_identity),
        *_flatten(layout.target_scope),
        *_flatten(layout.relationships),
    )


def _integer_compatibility(layout: _ProjectionLayout) -> str:
    checks = [
        f"{_identifier(value.alias)} IS NULL OR try_cast({_identifier(value.alias)} AS HUGEINT) IS NOT NULL"
        for value in _all_values(layout)
        if value.value_type == "integer"
    ]
    return " AND ".join(checks) or "TRUE"


def _source_display_compatibility(
    program: ColumnarTransformationProgram,
) -> str:
    checks = []
    for item in program.inputs:
        value = _identifier(source_value_column(item.ordinal))
        kind = _identifier(source_kind_column(item.ordinal))
        checks.append(
            f"({kind} <> {int(SourceCellKind.DECIMAL)} OR {value} NOT ILIKE '%e%')"
        )
        checks.append(
            f"({kind} <> {int(SourceCellKind.DATETIME)} OR "
            f"try_cast({value} AS TIMESTAMPTZ) IS NOT NULL)"
        )
    return " AND ".join(checks) or "TRUE"


def _control_totals(
    connection: duckdb.DuckDBPyConnection,
    path: str | Path,
    program: ColumnarTransformationProgram,
    layout: _ProjectionLayout,
    control_fields: tuple[str, ...],
) -> tuple[NativeControlTotalValue, ...] | None:
    if not control_fields:
        return ()
    by_field = {
        field.target_field: (field, value)
        for field, value in zip(program.scalar_fields, layout.scalars, strict=True)
    }
    results = []
    for target_field in control_fields:
        pair = by_field.get(target_field)
        if pair is None:
            return None
        field, value = pair
        column = _identifier(value.alias)
        if field.value_type == "integer":
            scale = 0
            integer_digits = connection.execute(
                f"""
                SELECT COALESCE(MAX(len(ltrim({column}, '+-0'))), 1), COUNT(*)
                  FROM read_parquet(?)
                 WHERE {column} IS NOT NULL
                """,
                [str(Path(path).resolve())],
            ).fetchone()
        elif field.value_type == "decimal":
            shape = connection.execute(
                f"""
                SELECT COALESCE(MAX(CASE WHEN contains(ltrim({column}, '+-'), '.')
                         THEN len(split_part(ltrim({column}, '+-'), '.', 2))
                         ELSE 0 END), 0),
                       COALESCE(MAX(len(COALESCE(NULLIF(ltrim(
                           split_part(ltrim({column}, '+-'), '.', 1), '0'
                       ), ''), '0'))), 1),
                       COUNT(*)
                  FROM read_parquet(?)
                 WHERE {column} IS NOT NULL
                """,
                [str(Path(path).resolve())],
            ).fetchone()
            if shape is None:
                return None
            scale = int(shape[0])
            integer_digits = (int(shape[1]), int(shape[2]))
        else:
            return None
        if integer_digits is None:
            return None
        max_integer_digits, included = int(integer_digits[0]), int(integer_digits[1])
        growth = len(str(max(included, 1)))
        if scale > 37 or max_integer_digits + growth > 38 - scale:
            return None
        row = connection.execute(
            f"""
            SELECT CAST(COALESCE(SUM(CAST({column} AS DECIMAL(38, {scale}))),
                                 CAST(0 AS DECIMAL(38, {scale}))) AS VARCHAR),
                   COUNT(*) FILTER (WHERE {column} IS NOT NULL),
                   COUNT(*) FILTER (WHERE {column} IS NULL)
              FROM read_parquet(?)
            """,
            [str(Path(path).resolve())],
        ).fetchone()
        if row is None:
            return None
        results.append(
            NativeControlTotalValue(
                target_field=target_field,
                actual_total=str(row[0]),
                included_rows=int(row[1]),
                empty_rows=int(row[2]),
            )
        )
    return tuple(results)


def _scalar_prepared_alias(index: int) -> str:
    return f"__impodo_scalar_prepared_{index:06d}"


def _scalar_value_alias(index: int) -> str:
    return f"__impodo_scalar_value_{index:06d}"


def _scalar_fallback_alias(index: int) -> str:
    return f"__impodo_scalar_fallback_{index:06d}"


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = [
    "NativeControlTotalValue",
    "NativePreparedProjectionResult",
    "append_clean_native_projection",
    "projected_encoded_rows_sql",
    "supports_clean_native_projection",
]
