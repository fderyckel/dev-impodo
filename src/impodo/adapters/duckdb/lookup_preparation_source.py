"""Produce a flat reusable list from one narrow frozen Parquet projection.

DuckDB owns grouping and physical membership. Python applies the authoritative
Unicode normalization once per distinct encoded value, in bounded pages.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from impodo.application.shared.columnar_runtime import configure_columnar_runtime

configure_columnar_runtime()

import duckdb

from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    GroupedExactJoinBatch,
)
from impodo.domain.preparation.source import SourceLoadError, SourceRow
from impodo.domain.shared.models import Issue, Severity
from impodo.domain.source_snapshot import (
    EncodedSourceCell,
    SourceCellKind,
    SourceSnapshot,
)
from impodo.domain.staging.scale import LOOKUP_DERIVED_OUTPUT_ROW_LIMIT
from impodo.domain.workspace.contracts import SourceDataset
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    DerivedEntityRule,
    _normalized_path,
)

from .constants import DUCKDB_JSON_BATCH_MAX_BYTES
from .serialization import iter_encoded_json_batches
from .unit_of_work import DuckDbConnectionFactory

_LOOKUP_LINEAGE_BATCH_BYTES = 32 * 1024 * 1024


def iter_lookup_batches(
    factory: DuckDbConnectionFactory,
    *,
    rule: DerivedEntityRule,
    link: DerivedDatasetLink,
    effective_dataset: SourceDataset,
    snapshot: SourceSnapshot,
    path: Path,
    batch_size: int,
) -> Iterator[GroupedExactJoinBatch]:
    """Scan one value column once and stream ordered keys with all their members."""

    if batch_size < 1 or rule.parent_separator is not None:
        raise SourceLoadError("The bounded lookup requires a flat value list")
    column = next(
        (
            item
            for item in snapshot.schema.columns
            if item.stable_key == rule.source_column_key
        ),
        None,
    )
    if column is None or snapshot.dataset_id != rule.source_dataset_id:
        raise SourceLoadError("The reusable list's frozen source field changed")
    with factory.connect(
        Path(":memory:"),
        memory_limit="256MB",
        threads="2",
        preserve_insertion_order=False,
        enable_external_access=True,
    ) as connection:
        try:
            # Identifiers come exclusively from the validated snapshot contract.
            connection.execute(
                f"""
                CREATE TEMP TABLE lookup_input AS
                SELECT "{snapshot.schema.source_row_column}" AS source_row,
                       "{column.value_column}" AS value, "{column.kind_column}" AS kind
                  FROM read_parquet(?)
            """,
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
                raise SourceLoadError("The reusable list's frozen source rows changed")
            connection.execute("""
                CREATE TEMP TABLE lookup_distinct AS
                SELECT ROW_NUMBER() OVER (ORDER BY MIN(source_row)) AS raw_id,
                       kind, value
                  FROM lookup_input GROUP BY kind, value
            """)
            connection.execute(
                "CREATE TEMP TABLE lookup_normalized (raw_id BIGINT, canonical_key VARCHAR, display VARCHAR)"
            )
            offset = 0
            while raw_batch := connection.execute(
                """
                SELECT raw_id, kind, value FROM lookup_distinct
                 WHERE raw_id > ? ORDER BY raw_id LIMIT ?
            """,
                [offset, batch_size],
            ).fetchall():
                normalized = []
                for raw_id, kind, value in raw_batch:
                    decoded = EncodedSourceCell(
                        SourceCellKind(int(kind)), value
                    ).to_python()
                    normalized_path = _normalized_path(decoded, None)
                    normalized.append(
                        {
                            "raw_id": int(raw_id),
                            "canonical_key": normalized_path[1][0]
                            if normalized_path
                            else None,
                            "display": normalized_path[0][0]
                            if normalized_path
                            else None,
                        }
                    )
                for encoded in iter_encoded_json_batches(
                    normalized,
                    max_rows=batch_size,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO lookup_normalized
                        SELECT item.raw_id, item.canonical_key, item.display
                          FROM (SELECT UNNEST(from_json_strict(CAST(? AS JSON), ?)) AS item)
                    """,
                        [
                            encoded.payload,
                            '[{"raw_id":"BIGINT","canonical_key":"VARCHAR","display":"VARCHAR"}]',
                        ],
                    )
                offset = int(raw_batch[-1][0])
            connection.execute("""
                CREATE TEMP TABLE lookup_members AS
                SELECT normalized.canonical_key, normalized.display, input.source_row
                  FROM lookup_input AS input
                  JOIN lookup_distinct AS raw
                    ON raw.kind = input.kind AND raw.value IS NOT DISTINCT FROM input.value
                  JOIN lookup_normalized AS normalized ON normalized.raw_id = raw.raw_id
                 WHERE normalized.canonical_key IS NOT NULL
            """)
            groups = connection.execute(
                "SELECT COUNT(DISTINCT canonical_key) FROM lookup_members"
            ).fetchone()
            if groups is None or int(groups[0]) > LOOKUP_DERIVED_OUTPUT_ROW_LIMIT:
                raise SourceLoadError(
                    "The reusable list exceeds its bounded generated-record budget"
                )
            cursor = connection.execute("""
                SELECT ROW_NUMBER() OVER (ORDER BY canonical_key) + 1 AS generated_row,
                       canonical_key, arg_min(display, source_row),
                       COUNT(DISTINCT display), MIN(source_row),
                       list(source_row ORDER BY source_row)
                  FROM lookup_members GROUP BY canonical_key ORDER BY canonical_key
            """)
            while batch := cursor.fetchmany(batch_size):
                if (
                    sum(len(item[5]) * 8 for item in batch)
                    > _LOOKUP_LINEAGE_BATCH_BYTES
                ):
                    raise SourceLoadError(
                        "The reusable list exceeds its bounded lineage transport budget"
                    )
                rows, lineages, findings = [], [], []
                for number, key, name, aliases, first_row, members in batch:
                    values = {
                        link.canonical_key_column_key: key,
                        link.name_column_key: name,
                    }
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
                                    "multiple source spellings produce the same related "
                                    "record; review the preferred display value "
                                    f"(first seen at source row {first_row})"
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
                yield GroupedExactJoinBatch(
                    tuple(rows), tuple(lineages), tuple(findings)
                )
        except duckdb.Error as error:
            raise SourceLoadError(
                "Impodo could not prepare the frozen reusable list"
            ) from error
