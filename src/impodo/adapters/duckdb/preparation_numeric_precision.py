"""Stream authoritative numeric checks into bounded, value-free SQL batches.

Source cursors own their result sets while the caller's connection publishes
failure batches. Final findings retain the existing bounded quality envelope;
this module does not claim constant memory for the complete preparation worker.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing

import duckdb

from impodo.application.shared.numeric_performance import (
    observe_numeric_failure_batch,
    observe_numeric_value_page,
    time_numeric_validation,
)
from impodo.domain.preparation.quality_budget import (
    QualityFindingBudget,
    QualityFindingBudgetExceeded,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.preparation_session import (
    PreparedCanonicalEncodingUnsupported,
    PreparedCanonicalProjection,
)
from impodo.domain.target_numeric_precision import (
    PreparedNumericField,
    PreparedNumericPrecisionFinding,
    prepared_numeric_precision_finding,
)
from impodo.domain.workspace.errors import WorkspaceError

from .native_prepared_projection import projected_numeric_values_sql
from .preparation_session_support import PreparationSessionRepositoryHost
from .serialization import iter_encoded_json_batches

NUMERIC_VALUE_PAGE_MAX_ROWS = 256
NUMERIC_VALUE_PAGE_TARGET_BYTES = 2 * 1024 * 1024
NUMERIC_FAILURE_BATCH_MAX_ROWS = 256
_FAILURE_STRUCTURE = """[{
    "ordinal":"BIGINT", "target_model":"VARCHAR", "field_name":"VARCHAR",
    "precision":"INTEGER", "scale":"INTEGER", "required_places":"INTEGER",
    "integral_overflow":"BOOLEAN", "zeroed_positive":"BOOLEAN"
}]"""
_STORED_VALUES_SQL = """
    SELECT row.ordinal, row.row_id, row.dataset, row.source_row,
           spec.target_model, spec.field_name, spec.precision, spec.scale,
           COALESCE(json_extract_string(value.value, '$.value'),
                    json_extract_string(value.value, '$')) AS prepared_value
      FROM canonical_staging_row AS row
      JOIN quality_numeric_field AS spec
        ON spec.dataset = row.dataset
       AND spec.target_model = json_extract_string(row.row_json, '$.target_model')
      CROSS JOIN json_each(json_extract(row.row_json, '$.proposed_values')) AS value
     WHERE row.run_id = ? AND row.row_json != ''
       AND row.disposition IN ('CANDIDATE', 'REFERENCE')
       AND value.key = spec.field_name
"""


def _bounded_value_query(query: str, columns: str, order: str) -> str:
    """Check a single cell's byte allowance before transferring its value."""

    return f"""
        WITH numeric_values AS ({query}), sized AS (
            SELECT *, COALESCE(octet_length(encode(prepared_value)), 0) AS value_bytes
              FROM numeric_values
        )
        SELECT {columns}, CASE WHEN value_bytes <= ? THEN prepared_value END,
               value_bytes
          FROM sized ORDER BY {order}
    """


class _ValuePages:
    """Observe bounded scan pages without accumulating their numeric values."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.cells = self.findings = self.byte_count = 0

    def add(self, byte_count: int, finding: bool) -> None:
        """Record a consumed cell, including a permitted single oversized cell."""

        if byte_count > self.max_bytes:
            raise QualityFindingBudgetExceeded(
                "A prepared numeric value exceeds the bounded data-check memory budget. "
                "No partial numeric evidence was published."
            )
        if self.cells and (
            self.cells >= NUMERIC_VALUE_PAGE_MAX_ROWS
            or self.byte_count + byte_count > NUMERIC_VALUE_PAGE_TARGET_BYTES
        ):
            self.flush()
        self.cells += 1
        self.findings += finding
        self.byte_count += byte_count

    def flush(self) -> None:
        """Merge actual page facts once, including interrupted scan coverage."""

        observe_numeric_value_page(
            cells=self.cells, findings=self.findings, byte_count=self.byte_count
        )
        self.cells = self.findings = self.byte_count = 0


def _failure(
    ordinal: int, finding: PreparedNumericPrecisionFinding
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "target_model": finding.target_model,
        "field_name": finding.field_name,
        "precision": finding.digits[0],
        "scale": finding.digits[1],
        "required_places": finding.required_decimal_places,
        "integral_overflow": finding.integral_overflow,
        "zeroed_positive": finding.zeroed_positive,
    }


def _iter_failures(
    host: PreparationSessionRepositoryHost,
    reader: duckdb.DuckDBPyConnection,
    projections: duckdb.DuckDBPyConnection,
    workspace_id: str,
    session_id: str,
    numeric_fields: tuple[PreparedNumericField, ...],
    max_bytes: int,
) -> Iterator[Mapping[str, object]]:
    specs = {
        (item.dataset, item.target_model, item.field_name): item
        for item in numeric_fields
    }
    if len(specs) != len(numeric_fields):
        raise WorkspaceError("Prepared numeric field evidence is ambiguous")
    reader.execute("""
        CREATE TEMP TABLE quality_numeric_field (
            dataset VARCHAR, target_model VARCHAR, field_name VARCHAR,
            precision INTEGER, scale INTEGER,
            PRIMARY KEY (dataset, target_model, field_name)
        )
    """)
    for batch in iter_encoded_json_batches(
        (
            {
                "dataset": item.dataset,
                "model": item.target_model,
                "field": item.field_name,
                "precision": item.digits[0],
                "scale": item.digits[1],
            }
            for item in numeric_fields
        ),
        max_rows=NUMERIC_VALUE_PAGE_MAX_ROWS,
        max_bytes=max_bytes,
    ):
        reader.execute(
            """
            INSERT INTO quality_numeric_field
            SELECT value->>'dataset', value->>'model', value->>'field',
                   CAST(value->>'precision' AS INTEGER), CAST(value->>'scale' AS INTEGER)
              FROM json_each(?)
        """,
            [batch.payload],
        )
    pages = _ValuePages(max_bytes)
    try:
        reader.execute(
            _bounded_value_query(
                _STORED_VALUES_SQL,
                "ordinal, row_id, dataset, source_row, target_model, field_name, precision, scale",
                "ordinal, field_name",
            ),
            [session_id, max_bytes],
        )
        while raw := reader.fetchone():
            (
                ordinal,
                row_id,
                dataset,
                source_row,
                model,
                field_name,
                precision,
                scale,
                value,
                value_bytes,
            ) = raw
            size = int(value_bytes)
            if size > max_bytes:
                pages.add(size, False)
            finding = prepared_numeric_precision_finding(
                row_id=str(row_id),
                dataset=str(dataset),
                source_row=int(source_row),
                target_model=str(model),
                field_name=str(field_name),
                digits=(int(precision), int(scale)),
                value=value,
            )
            pages.add(size, finding is not None)
            if finding is not None:
                yield _failure(int(ordinal), finding)

        projections.execute(
            """
            SELECT projection.projection_json, manifest.manifest_json
              FROM canonical_prepared_projection AS projection
              JOIN prepared_snapshot_manifest AS manifest
                ON manifest.content_hash = projection.prepared_snapshot_hash
               AND manifest.dataset_id = projection.dataset_id
             WHERE projection.run_id = ? ORDER BY projection.ordinal_start
        """,
            [session_id],
        )
        while raw_projection := projections.fetchone():
            try:
                projection = PreparedCanonicalProjection.from_portable_dict(
                    json.loads(str(raw_projection[0]))
                )
            except PreparedCanonicalEncodingUnsupported as error:
                raise WorkspaceError(str(error)) from error
            fields = tuple(
                sorted(
                    item.field_name
                    for item in numeric_fields
                    if item.dataset == projection.dataset
                    and item.target_model == projection.program.target_model
                )
            )
            if not fields:
                continue
            snapshot = PreparedSnapshot.from_json(str(raw_projection[1]))
            if (
                snapshot.workspace_id != workspace_id
                or snapshot.dataset_id != projection.dataset_id
                or snapshot.row_count != projection.row_count
                or snapshot.transformation_program_hash
                != projection.program.content_hash
                or snapshot.mapping_hash != projection.program.mapping_content_hash
                or snapshot.schema_hash != projection.program.schema_hash
            ):
                raise WorkspaceError("Prepared canonical projection metadata changed")
            with host._artifacts.materialize_prepared_snapshot(
                workspace_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                reader.execute(
                    _bounded_value_query(
                        projected_numeric_values_sql(projection, fields),
                        "prepared_ordinal, field_name",
                        "prepared_ordinal, field_name",
                    ),
                    [str(path), max_bytes],
                )
                while raw_value := reader.fetchone():
                    ordinal, field_name, value, value_bytes = raw_value
                    size = int(value_bytes)
                    if size > max_bytes:
                        pages.add(size, False)
                    spec = specs.get(
                        (
                            projection.dataset,
                            projection.program.target_model,
                            str(field_name),
                        )
                    )
                    if spec is None:
                        raise WorkspaceError("Prepared numeric field evidence changed")
                    finding = prepared_numeric_precision_finding(
                        row_id="pending",
                        dataset=projection.dataset,
                        source_row=1,
                        target_model=spec.target_model,
                        field_name=spec.field_name,
                        digits=spec.digits,
                        value=value,
                    )
                    pages.add(size, finding is not None)
                    if finding is not None:
                        yield _failure(projection.ordinal_start + int(ordinal), finding)
    finally:
        pages.flush()


def bounded_numeric_precision_rows(
    host: PreparationSessionRepositoryHost,
    connection: duckdb.DuckDBPyConnection,
    workspace_id: str,
    session_id: str,
    numeric_fields: tuple[PreparedNumericField, ...],
    *,
    max_bytes: int,
    read_findings: Callable[
        [duckdb.DuckDBPyConnection], tuple[tuple[object, ...], ...]
    ],
) -> tuple[tuple[object, ...], ...]:
    """Evaluate all source routes and retain only the bounded finding envelope."""

    if not numeric_fields:
        return ()
    temporary_budget = QualityFindingBudget(limit_bytes=max_bytes)
    with (
        time_numeric_validation(),
        closing(connection.cursor()) as reader,
        closing(connection.cursor()) as projections,
    ):
        connection.execute("""
            CREATE TEMP TABLE quality_numeric_failure (
                ordinal BIGINT, target_model VARCHAR, field_name VARCHAR,
                precision INTEGER, scale INTEGER, required_places INTEGER,
                integral_overflow BOOLEAN, zeroed_positive BOOLEAN,
                PRIMARY KEY (ordinal, field_name)
            )
        """)

        def failures() -> Iterator[Mapping[str, object]]:
            for finding in _iter_failures(
                host,
                reader,
                projections,
                workspace_id,
                session_id,
                numeric_fields,
                max_bytes,
            ):
                temporary_budget.consume(
                    512 + len(json.dumps(finding, ensure_ascii=False).encode("utf-8"))
                )
                yield finding

        try:
            for batch in iter_encoded_json_batches(
                failures(),
                max_rows=NUMERIC_FAILURE_BATCH_MAX_ROWS,
                max_bytes=max_bytes,
            ):
                connection.execute(
                    """
                    INSERT INTO quality_numeric_failure
                    SELECT item.ordinal, item.target_model, item.field_name,
                           item.precision, item.scale, item.required_places,
                           item.integral_overflow, item.zeroed_positive
                      FROM (SELECT UNNEST(from_json_strict(CAST(? AS JSON), ?)) AS item)
                """,
                    [batch.payload, _FAILURE_STRUCTURE],
                )
                observe_numeric_failure_batch(
                    rows=batch.row_count, byte_count=batch.byte_count
                )
            return read_findings(
                connection.execute(
                    """
                SELECT row.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, lineage.physical_dataset_id,
                       lineage.physical_source_row, failure.target_model,
                       failure.field_name, failure.precision, failure.scale,
                       failure.required_places, failure.integral_overflow,
                       failure.zeroed_positive
                  FROM quality_numeric_failure AS failure
                  JOIN canonical_staging_row AS row
                    ON row.run_id = ? AND row.ordinal = failure.ordinal
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE row.disposition IN ('CANDIDATE', 'REFERENCE')
                 ORDER BY row.ordinal, failure.field_name
            """,
                    [session_id],
                )
            )
        except QualityFindingBudgetExceeded as error:
            raise WorkspaceError(str(error)) from error
        finally:
            connection.execute("DROP TABLE quality_numeric_failure")
