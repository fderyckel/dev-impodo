"""Compare bounded Python-to-DuckDB transports for high-volume evidence.

The timer includes construction of each transport payload and the database
insert. Output contains only row counts, byte counts, and elapsed time.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from statistics import median
from time import perf_counter
from typing import Callable, Sequence

import duckdb

from impodo.adapters.duckdb.constants import (
    DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
    DUCKDB_JSON_BATCH_MAX_BYTES,
)
from impodo.adapters.duckdb.normalization_repository import (
    _NORMALIZATION_EFFECT_JSON_STRUCTURE,
)
from impodo.adapters.duckdb.preparation_session_repository import (
    _CANONICAL_STAGING_ROW_JSON_STRUCTURE,
    _PREPARATION_IMPACT_JSON_STRUCTURE,
)
from impodo.adapters.duckdb.quality_repository import (
    _QUALITY_ROW_RESULT_JSON_STRUCTURE,
    _SOURCE_ACCOUNTING_ENTRY_JSON_STRUCTURE,
    _SOURCE_ACCOUNTING_LINK_JSON_STRUCTURE,
)
from impodo.adapters.duckdb.serialization import (
    _canonical_json,
    _columnar_parameters,
    iter_encoded_json_batches,
)
from impodo.adapters.duckdb.unit_of_work import DUCKDB_CONFIG


_EFFECT_FIELDS = (
    "effect_id",
    "group_id",
    "row_id",
    "dataset",
    "source_row",
    "target_field",
    "eligible",
    "effect_json",
)
_QUALITY_ROW_FIELDS = (
    "run_id",
    "ordinal",
    "row_id",
    "dataset",
    "source_row",
    "effective_disposition",
    "requires_review",
    "row_json",
)
_SOURCE_ACCOUNTING_ENTRY_FIELDS = (
    "run_id",
    "ordinal",
    "physical_dataset_id",
    "source_row",
    "state",
    "entry_json",
)
_SOURCE_ACCOUNTING_LINK_FIELDS = (
    "run_id",
    "accounting_ordinal",
    "row_id",
)
_PREPARATION_IMPACT_FIELDS = (
    "session_id",
    "ordinal",
    "dataset",
    "source_row",
    "target_field",
    "outcome",
    "impact_json",
)
_CANONICAL_ROW_FIELDS = (
    "run_id",
    "ordinal",
    "row_id",
    "dataset",
    "source_row",
    "target_model",
    "disposition",
    "row_json",
)


@dataclass(frozen=True, slots=True)
class SyntheticEffect:
    """One deterministic, non-customer effect-shaped benchmark input."""

    ordinal: int

    def transport_row(self) -> dict[str, object]:
        """Construct the same fixed transport shape used by normalization."""

        effect_id = f"effect-{self.ordinal:08d}"
        dataset = "products"
        target_field = f"field_{self.ordinal % 20:02d}"
        portable = {
            "after": f"normalized-{self.ordinal % 97:02d}",
            "before": f"raw-{self.ordinal % 97:02d}",
            "dataset": dataset,
            "effect_id": effect_id,
            "eligible": self.ordinal % 11 != 0,
            "group_id": f"group-{self.ordinal % 31:04d}",
            "row_id": f"row-{self.ordinal // 20:08d}",
            "source_row": self.ordinal // 20 + 1,
            "target_field": target_field,
        }
        return {
            "effect_id": effect_id,
            "group_id": portable["group_id"],
            "row_id": portable["row_id"],
            "dataset": dataset,
            "source_row": portable["source_row"],
            "target_field": target_field,
            "eligible": portable["eligible"],
            "effect_json": _canonical_json(portable),
        }


@dataclass(frozen=True, slots=True)
class SyntheticQualityRow:
    """One deterministic, non-customer quality-row benchmark input."""

    ordinal: int

    def transport_row(self) -> dict[str, object]:
        """Construct the fixed quality row shape before adapter transport."""

        row_id = f"row-{self.ordinal:08d}"
        dataset = "products"
        portable = {
            "dataset": dataset,
            "effective_disposition": (
                "QUARANTINED" if self.ordinal % 23 == 0 else "CANDIDATE"
            ),
            "issues": [],
            "requires_review": self.ordinal % 17 == 0,
            "row_id": row_id,
            "source_row": self.ordinal + 1,
        }
        return {
            "run_id": "benchmark-run",
            "ordinal": self.ordinal,
            "row_id": row_id,
            "dataset": dataset,
            "source_row": self.ordinal + 1,
            "effective_disposition": portable["effective_disposition"],
            "requires_review": portable["requires_review"],
            "row_json": _canonical_json(portable),
        }


@dataclass(frozen=True, slots=True)
class SyntheticSourceAccounting:
    """One deterministic source-ledger entry and its optional row link."""

    ordinal: int

    @property
    def represented(self) -> bool:
        """Keep some no-link states in the synthetic type-fidelity shape."""

        return self.ordinal % 29 != 0

    def entry_transport_row(self) -> dict[str, object]:
        """Construct the source-accounting entry transport shape."""

        row_ids = (
            [f"row-{self.ordinal:08d}"]
            if self.represented
            else []
        )
        portable = {
            "canonical_row_ids": row_ids,
            "physical_dataset_id": "dataset:products",
            "source_row": self.ordinal + 1,
            "state": "REPRESENTED" if self.represented else "UNREPRESENTED",
        }
        return {
            "run_id": "benchmark-run",
            "ordinal": self.ordinal,
            "physical_dataset_id": portable["physical_dataset_id"],
            "source_row": portable["source_row"],
            "state": portable["state"],
            "entry_json": _canonical_json(portable),
        }

    def link_transport_rows(self) -> tuple[dict[str, object], ...]:
        """Return the zero-or-one row-link shape for this synthetic entry."""

        if not self.represented:
            return ()
        return ({
            "run_id": "benchmark-run",
            "accounting_ordinal": self.ordinal,
            "row_id": f"row-{self.ordinal:08d}",
        },)


@dataclass(frozen=True, slots=True)
class SyntheticPreparationImpact:
    """One deterministic, non-customer preparation impact input."""

    ordinal: int

    def transport_row(self) -> dict[str, object]:
        """Construct the exact preparation-impact adapter shape."""

        dataset = "products"
        target_field = f"field_{self.ordinal % 20:02d}"
        portable = {
            "dataset": dataset,
            "message": "",
            "outcome": "changed",
            "proposed_value": f"normalized-{self.ordinal % 97:02d}",
            "raw_value": f"raw-{self.ordinal % 97:02d}",
            "rules": "Trim + normalize",
            "source_column": f"Column {self.ordinal % 20:02d}",
            "source_row": self.ordinal // 20 + 1,
            "target_field": target_field,
        }
        return {
            "session_id": "benchmark-session",
            "ordinal": self.ordinal,
            "dataset": dataset,
            "source_row": portable["source_row"],
            "target_field": target_field,
            "outcome": portable["outcome"],
            "impact_json": _canonical_json(portable),
        }


@dataclass(frozen=True, slots=True)
class SyntheticCanonicalRow:
    """One deterministic canonical row with a representative JSON body."""

    ordinal: int

    def transport_row(self) -> dict[str, object]:
        """Construct the direct canonical staging transport shape."""

        row_id = f"row-{self.ordinal:08d}"
        dataset = "products"
        portable = {
            "dataset": dataset,
            "disposition": "CANDIDATE",
            "issues": [],
            "lineage": {
                "physical_sources": {
                    "dataset:products": [self.ordinal + 1]
                }
            },
            "proposed_values": {
                f"field_{index:02d}": f"value-{self.ordinal % 97:02d}-{index:02d}"
                for index in range(20)
            },
            "row_id": row_id,
            "source_identity": {"sku": f"SKU-{self.ordinal:08d}"},
            "source_row": self.ordinal + 1,
            "target_model": "product.template",
        }
        return {
            "run_id": "benchmark-run",
            "ordinal": self.ordinal,
            "row_id": row_id,
            "dataset": dataset,
            "source_row": self.ordinal + 1,
            "target_model": portable["target_model"],
            "disposition": portable["disposition"],
            "row_json": _canonical_json(portable),
        }
@dataclass(frozen=True, slots=True)
class TransportObservation:
    """Non-sensitive result of one transport benchmark round."""

    transport: str
    row_count: int
    batch_count: int
    transport_bytes: int
    elapsed_seconds: float
    related_row_count: int = 0


def _connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:", config=dict(DUCKDB_CONFIG))
    connection.execute(
        """
        CREATE TABLE effect_transport (
            effect_id VARCHAR NOT NULL,
            group_id VARCHAR NOT NULL,
            row_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            target_field VARCHAR NOT NULL,
            eligible BOOLEAN NOT NULL,
            effect_json VARCHAR NOT NULL
        )
        """
    )
    return connection


def _validate_insert(
    connection: duckdb.DuckDBPyConnection,
    effects: Sequence[SyntheticEffect],
) -> int:
    """Check the inserted count and one complete row without printing values."""

    row_count = int(
        connection.execute("SELECT COUNT(*) FROM effect_transport").fetchone()[0]
    )
    expected = effects[0].transport_row()
    actual = connection.execute(
        """
        SELECT effect_id, group_id, row_id, dataset, source_row,
               target_field, eligible, effect_json
          FROM effect_transport
         ORDER BY effect_id
         LIMIT 1
        """
    ).fetchone()
    if actual != tuple(expected[field] for field in _EFFECT_FIELDS):
        raise RuntimeError("Transport benchmark changed the typed row shape")
    return row_count


def _column_arrays(
    effects: Sequence[SyntheticEffect],
    batch_size: int,
) -> TransportObservation:
    connection = _connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, len(effects), batch_size):
            rows = [
                tuple(row[field] for field in _EFFECT_FIELDS)
                for effect in effects[start : start + batch_size]
                for row in (effect.transport_row(),)
            ]
            transport_bytes += sum(
                len(str(value).encode("utf-8"))
                for row in rows
                for value in row
            )
            connection.execute(
                """
                INSERT INTO effect_transport
                SELECT
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BOOLEAN),
                    CAST(UNNEST(?) AS VARCHAR)
                """,
                _columnar_parameters(rows),
            )
            batch_count += 1
        elapsed_seconds = perf_counter() - started
        row_count = _validate_insert(connection, effects)
    finally:
        connection.close()
    return TransportObservation(
        transport="column_arrays",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _typed_json(
    effects: Sequence[SyntheticEffect],
    batch_size: int,
) -> TransportObservation:
    connection = _connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        rows = (effect.transport_row() for effect in effects)
        for batch in iter_encoded_json_batches(
            rows,
            max_rows=batch_size,
            max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
        ):
            connection.execute(
                """
                INSERT INTO effect_transport
                SELECT
                    item.effect_id,
                    item.group_id,
                    item.row_id,
                    item.dataset,
                    item.source_row,
                    item.target_field,
                    item.eligible,
                    item.effect_json
                  FROM (
                    SELECT UNNEST(
                        from_json_strict(CAST(? AS JSON), ?)
                    ) AS item
                  )
                """,
                [batch.payload, _NORMALIZATION_EFFECT_JSON_STRUCTURE],
            )
            batch_count += 1
            transport_bytes += batch.byte_count
        elapsed_seconds = perf_counter() - started
        row_count = _validate_insert(connection, effects)
    finally:
        connection.close()
    return TransportObservation(
        transport="typed_json",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _quality_connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:", config=dict(DUCKDB_CONFIG))
    connection.execute(
        """
        CREATE TABLE quality_row_transport (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            row_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            effective_disposition VARCHAR NOT NULL,
            requires_review BOOLEAN NOT NULL,
            row_json VARCHAR NOT NULL
        )
        """
    )
    return connection


def _validate_quality_insert(
    connection: duckdb.DuckDBPyConnection,
    rows: Sequence[SyntheticQualityRow],
) -> int:
    """Check quality-row count and types without printing synthetic values."""

    row_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM quality_row_transport"
        ).fetchone()[0]
    )
    expected = rows[0].transport_row()
    actual = connection.execute(
        """
        SELECT run_id, ordinal, row_id, dataset, source_row,
               effective_disposition, requires_review, row_json
          FROM quality_row_transport
         ORDER BY ordinal
         LIMIT 1
        """
    ).fetchone()
    if actual != tuple(expected[field] for field in _QUALITY_ROW_FIELDS):
        raise RuntimeError("Quality benchmark changed the typed row shape")
    return row_count


def _quality_column_arrays(
    quality_rows: Sequence[SyntheticQualityRow],
    batch_size: int,
) -> TransportObservation:
    connection = _quality_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, len(quality_rows), batch_size):
            rows = [
                tuple(row[field] for field in _QUALITY_ROW_FIELDS)
                for item in quality_rows[start : start + batch_size]
                for row in (item.transport_row(),)
            ]
            transport_bytes += sum(
                len(str(value).encode("utf-8"))
                for row in rows
                for value in row
            )
            connection.execute(
                """
                INSERT INTO quality_row_transport
                SELECT
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BOOLEAN),
                    CAST(UNNEST(?) AS VARCHAR)
                """,
                _columnar_parameters(rows),
            )
            batch_count += 1
        elapsed_seconds = perf_counter() - started
        row_count = _validate_quality_insert(connection, quality_rows)
    finally:
        connection.close()
    return TransportObservation(
        transport="column_arrays",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _quality_typed_json(
    quality_rows: Sequence[SyntheticQualityRow],
    batch_size: int,
) -> TransportObservation:
    connection = _quality_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        rows = (
            {
                **{
                    field: row[field]
                    for field in _QUALITY_ROW_FIELDS
                    if field != "run_id"
                },
                "record_label": f"Product {item.ordinal:08d}",
                "base_disposition": "CANDIDATE",
            }
            for item in quality_rows
            for row in (item.transport_row(),)
        )
        for batch in iter_encoded_json_batches(
            rows,
            max_rows=batch_size,
            max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
        ):
            connection.execute(
                """
                INSERT INTO quality_row_transport
                SELECT
                    ?, item.ordinal, item.row_id, item.dataset,
                    item.source_row, item.effective_disposition,
                    item.requires_review, item.row_json
                  FROM (
                    SELECT UNNEST(
                        from_json_strict(CAST(? AS JSON), ?)
                    ) AS item
                  )
                """,
                [
                    "benchmark-run",
                    batch.payload,
                    _QUALITY_ROW_RESULT_JSON_STRUCTURE,
                ],
            )
            batch_count += 1
            transport_bytes += batch.byte_count
        elapsed_seconds = perf_counter() - started
        row_count = _validate_quality_insert(connection, quality_rows)
    finally:
        connection.close()
    return TransportObservation(
        transport="typed_json",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _source_accounting_connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:", config=dict(DUCKDB_CONFIG))
    connection.execute(
        """
        CREATE TABLE source_entry_transport (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            physical_dataset_id VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            state VARCHAR NOT NULL,
            entry_json VARCHAR NOT NULL
        );
        CREATE TABLE source_link_transport (
            run_id VARCHAR NOT NULL,
            accounting_ordinal BIGINT NOT NULL,
            row_id VARCHAR NOT NULL
        )
        """
    )
    return connection


def _validate_source_accounting_insert(
    connection: duckdb.DuckDBPyConnection,
    entries: Sequence[SyntheticSourceAccounting],
) -> tuple[int, int]:
    """Check both source-ledger tables without printing synthetic values."""

    entry_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM source_entry_transport"
        ).fetchone()[0]
    )
    link_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM source_link_transport"
        ).fetchone()[0]
    )
    expected_entry = entries[0].entry_transport_row()
    actual_entry = connection.execute(
        """
        SELECT run_id, ordinal, physical_dataset_id, source_row,
               state, entry_json
          FROM source_entry_transport
         ORDER BY ordinal
         LIMIT 1
        """
    ).fetchone()
    if actual_entry != tuple(
        expected_entry[field]
        for field in _SOURCE_ACCOUNTING_ENTRY_FIELDS
    ):
        raise RuntimeError("Source benchmark changed the entry row shape")
    first_linked = next(item for item in entries if item.represented)
    expected_link = first_linked.link_transport_rows()[0]
    actual_link = connection.execute(
        """
        SELECT run_id, accounting_ordinal, row_id
          FROM source_link_transport
         ORDER BY accounting_ordinal
         LIMIT 1
        """
    ).fetchone()
    if actual_link != tuple(
        expected_link[field]
        for field in _SOURCE_ACCOUNTING_LINK_FIELDS
    ):
        raise RuntimeError("Source benchmark changed the link row shape")
    return entry_count, link_count


def _source_accounting_column_arrays(
    entries: Sequence[SyntheticSourceAccounting],
    batch_size: int,
) -> TransportObservation:
    connection = _source_accounting_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, len(entries), batch_size):
            batch = entries[start : start + batch_size]
            entry_rows = [
                tuple(row[field] for field in _SOURCE_ACCOUNTING_ENTRY_FIELDS)
                for item in batch
                for row in (item.entry_transport_row(),)
            ]
            connection.execute(
                """
                INSERT INTO source_entry_transport
                SELECT
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR)
                """,
                _columnar_parameters(entry_rows),
            )
            batch_count += 1
            link_rows = [
                tuple(row[field] for field in _SOURCE_ACCOUNTING_LINK_FIELDS)
                for item in batch
                for row in item.link_transport_rows()
            ]
            if link_rows:
                connection.execute(
                    """
                    INSERT INTO source_link_transport
                    SELECT
                        CAST(UNNEST(?) AS VARCHAR),
                        CAST(UNNEST(?) AS BIGINT),
                        CAST(UNNEST(?) AS VARCHAR)
                    """,
                    _columnar_parameters(link_rows),
                )
                batch_count += 1
            transport_bytes += sum(
                len(str(value).encode("utf-8"))
                for row in (*entry_rows, *link_rows)
                for value in row
            )
        elapsed_seconds = perf_counter() - started
        entry_count, link_count = _validate_source_accounting_insert(
            connection,
            entries,
        )
    finally:
        connection.close()
    return TransportObservation(
        transport="column_arrays",
        row_count=entry_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
        related_row_count=link_count,
    )


def _source_accounting_typed_json(
    entries: Sequence[SyntheticSourceAccounting],
    batch_size: int,
) -> TransportObservation:
    connection = _source_accounting_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, len(entries), batch_size):
            batch = entries[start : start + batch_size]
            entry_rows = (
                {
                    field: row[field]
                    for field in _SOURCE_ACCOUNTING_ENTRY_FIELDS
                    if field != "run_id"
                }
                for item in batch
                for row in (item.entry_transport_row(),)
            )
            for encoded_batch in iter_encoded_json_batches(
                entry_rows,
                max_rows=batch_size,
                max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
            ):
                connection.execute(
                    """
                    INSERT INTO source_entry_transport
                    SELECT
                        ?, item.ordinal, item.physical_dataset_id,
                        item.source_row, item.state, item.entry_json
                      FROM (
                        SELECT UNNEST(
                            from_json_strict(CAST(? AS JSON), ?)
                        ) AS item
                      )
                    """,
                    [
                        "benchmark-run",
                        encoded_batch.payload,
                        _SOURCE_ACCOUNTING_ENTRY_JSON_STRUCTURE,
                    ],
                )
                batch_count += 1
                transport_bytes += encoded_batch.byte_count
            link_rows = (
                {
                    field: row[field]
                    for field in _SOURCE_ACCOUNTING_LINK_FIELDS
                    if field != "run_id"
                }
                for item in batch
                for link in item.link_transport_rows()
                for row in (link,)
            )
            for encoded_batch in iter_encoded_json_batches(
                link_rows,
                max_rows=batch_size,
                max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
            ):
                connection.execute(
                    """
                    INSERT INTO source_link_transport
                    SELECT ?, item.accounting_ordinal, item.row_id
                      FROM (
                        SELECT UNNEST(
                            from_json_strict(CAST(? AS JSON), ?)
                        ) AS item
                      )
                    """,
                    [
                        "benchmark-run",
                        encoded_batch.payload,
                        _SOURCE_ACCOUNTING_LINK_JSON_STRUCTURE,
                    ],
                )
                batch_count += 1
                transport_bytes += encoded_batch.byte_count
        elapsed_seconds = perf_counter() - started
        entry_count, link_count = _validate_source_accounting_insert(
            connection,
            entries,
        )
    finally:
        connection.close()
    return TransportObservation(
        transport="typed_json",
        row_count=entry_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
        related_row_count=link_count,
    )


def _impact_connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:", config=dict(DUCKDB_CONFIG))
    connection.execute(
        """
        CREATE TABLE impact_transport (
            session_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            target_field VARCHAR NOT NULL,
            outcome VARCHAR NOT NULL,
            impact_json VARCHAR NOT NULL
        )
        """
    )
    return connection


def _validate_impact_insert(
    connection: duckdb.DuckDBPyConnection,
    impacts: Sequence[SyntheticPreparationImpact],
) -> int:
    """Check impact count and one complete typed row without value output."""

    row_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM impact_transport"
        ).fetchone()[0]
    )
    expected = impacts[0].transport_row()
    actual = connection.execute(
        """
        SELECT session_id, ordinal, dataset, source_row,
               target_field, outcome, impact_json
          FROM impact_transport
         ORDER BY ordinal
         LIMIT 1
        """
    ).fetchone()
    if actual != tuple(
        expected[field]
        for field in _PREPARATION_IMPACT_FIELDS
    ):
        raise RuntimeError("Impact benchmark changed the typed row shape")
    return row_count


def _impact_column_arrays(
    impacts: Sequence[SyntheticPreparationImpact],
    batch_size: int,
) -> TransportObservation:
    connection = _impact_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, len(impacts), batch_size):
            rows = [
                tuple(row[field] for field in _PREPARATION_IMPACT_FIELDS)
                for item in impacts[start : start + batch_size]
                for row in (item.transport_row(),)
            ]
            connection.execute(
                """
                INSERT INTO impact_transport
                SELECT
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR)
                """,
                _columnar_parameters(rows),
            )
            batch_count += 1
            transport_bytes += sum(
                len(str(value).encode("utf-8"))
                for row in rows
                for value in row
            )
        elapsed_seconds = perf_counter() - started
        row_count = _validate_impact_insert(connection, impacts)
    finally:
        connection.close()
    return TransportObservation(
        transport="column_arrays",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _impact_typed_json(
    impacts: Sequence[SyntheticPreparationImpact],
    batch_size: int,
) -> TransportObservation:
    connection = _impact_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        rows = (
            {
                field: row[field]
                for field in _PREPARATION_IMPACT_FIELDS
                if field != "session_id"
            }
            for item in impacts
            for row in (item.transport_row(),)
        )
        for encoded_batch in iter_encoded_json_batches(
            rows,
            max_rows=batch_size,
            max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
        ):
            connection.execute(
                """
                INSERT INTO impact_transport
                SELECT
                    ?, item.ordinal, item.dataset, item.source_row,
                    item.target_field, item.outcome, item.impact_json
                  FROM (
                    SELECT UNNEST(
                        from_json_strict(CAST(? AS JSON), ?)
                    ) AS item
                  )
                """,
                [
                    "benchmark-session",
                    encoded_batch.payload,
                    _PREPARATION_IMPACT_JSON_STRUCTURE,
                ],
            )
            batch_count += 1
            transport_bytes += encoded_batch.byte_count
        elapsed_seconds = perf_counter() - started
        row_count = _validate_impact_insert(connection, impacts)
    finally:
        connection.close()
    return TransportObservation(
        transport="typed_json",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _canonical_connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:", config=dict(DUCKDB_CONFIG))
    connection.execute(
        """
        CREATE TABLE canonical_row_transport (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            row_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            target_model VARCHAR NOT NULL,
            disposition VARCHAR NOT NULL,
            row_json VARCHAR NOT NULL
        )
        """
    )
    return connection


def _validate_canonical_insert(
    connection: duckdb.DuckDBPyConnection,
    rows: Sequence[SyntheticCanonicalRow],
) -> int:
    """Check canonical count and one full typed row without value output."""

    row_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM canonical_row_transport"
        ).fetchone()[0]
    )
    expected = rows[0].transport_row()
    actual = connection.execute(
        """
        SELECT run_id, ordinal, row_id, dataset, source_row,
               target_model, disposition, row_json
          FROM canonical_row_transport
         ORDER BY ordinal
         LIMIT 1
        """
    ).fetchone()
    if actual != tuple(expected[field] for field in _CANONICAL_ROW_FIELDS):
        raise RuntimeError("Canonical benchmark changed the typed row shape")
    return row_count


def _canonical_column_arrays(
    canonical_rows: Sequence[SyntheticCanonicalRow],
    batch_size: int,
) -> TransportObservation:
    connection = _canonical_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, len(canonical_rows), batch_size):
            rows = [
                tuple(row[field] for field in _CANONICAL_ROW_FIELDS)
                for item in canonical_rows[start : start + batch_size]
                for row in (item.transport_row(),)
            ]
            connection.execute(
                """
                INSERT INTO canonical_row_transport
                SELECT
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS BIGINT),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR),
                    CAST(UNNEST(?) AS VARCHAR)
                """,
                _columnar_parameters(rows),
            )
            batch_count += 1
            transport_bytes += sum(
                len(str(value).encode("utf-8"))
                for row in rows
                for value in row
            )
        elapsed_seconds = perf_counter() - started
        row_count = _validate_canonical_insert(connection, canonical_rows)
    finally:
        connection.close()
    return TransportObservation(
        transport="column_arrays",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def _canonical_typed_json(
    canonical_rows: Sequence[SyntheticCanonicalRow],
    batch_size: int,
) -> TransportObservation:
    connection = _canonical_connection()
    batch_count = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        rows = (
            {
                **{
                    field: row[field]
                    for field in _CANONICAL_ROW_FIELDS
                    if field != "run_id"
                },
                "record_label": f"Product {item.ordinal:08d}",
                "quality_identity_key": None,
            }
            for item in canonical_rows
            for row in (item.transport_row(),)
        )
        for encoded_batch in iter_encoded_json_batches(
            rows,
            max_rows=batch_size,
            max_bytes=DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
        ):
            connection.execute(
                """
                INSERT INTO canonical_row_transport
                SELECT
                    ?, item.ordinal, item.row_id, item.dataset,
                    item.source_row, item.target_model,
                    item.disposition, item.row_json
                  FROM (
                    SELECT UNNEST(
                        from_json_strict(CAST(? AS JSON), ?)
                    ) AS item
                  )
                """,
                [
                    "benchmark-run",
                    encoded_batch.payload,
                    _CANONICAL_STAGING_ROW_JSON_STRUCTURE,
                ],
            )
            batch_count += 1
            transport_bytes += encoded_batch.byte_count
        elapsed_seconds = perf_counter() - started
        row_count = _validate_canonical_insert(connection, canonical_rows)
    finally:
        connection.close()
    return TransportObservation(
        transport="typed_json",
        row_count=row_count,
        batch_count=batch_count,
        transport_bytes=transport_bytes,
        elapsed_seconds=elapsed_seconds,
    )


def run_benchmark(
    *,
    row_count: int = 15_873,
    batch_size: int = 1_000,
    rounds: int = 3,
) -> tuple[TransportObservation, ...]:
    """Run both transports in alternating order for the requested rounds."""

    if row_count < 1 or batch_size < 1 or rounds < 1:
        raise ValueError("Benchmark rows, batch size, and rounds must be positive")
    effects = tuple(SyntheticEffect(index) for index in range(row_count))
    runners: tuple[
        tuple[str, Callable[[Sequence[SyntheticEffect], int], TransportObservation]],
        ...,
    ] = (
        ("column_arrays", _column_arrays),
        ("typed_json", _typed_json),
    )
    observations: list[TransportObservation] = []
    for round_index in range(rounds):
        ordered = runners if round_index % 2 == 0 else tuple(reversed(runners))
        for _name, runner in ordered:
            observation = runner(effects, batch_size)
            if observation.row_count != row_count:
                raise RuntimeError("Transport benchmark inserted an incomplete row set")
            observations.append(observation)
    return tuple(observations)


def run_quality_row_benchmark(
    *,
    row_count: int = 4_000,
    batch_size: int = 1_000,
    rounds: int = 3,
) -> tuple[TransportObservation, ...]:
    """Run current and typed transports for the quality-row family."""

    if row_count < 1 or batch_size < 1 or rounds < 1:
        raise ValueError("Benchmark rows, batch size, and rounds must be positive")
    quality_rows = tuple(SyntheticQualityRow(index) for index in range(row_count))
    runners: tuple[
        tuple[
            str,
            Callable[
                [Sequence[SyntheticQualityRow], int],
                TransportObservation,
            ],
        ],
        ...,
    ] = (
        ("column_arrays", _quality_column_arrays),
        ("typed_json", _quality_typed_json),
    )
    observations: list[TransportObservation] = []
    for round_index in range(rounds):
        ordered = runners if round_index % 2 == 0 else tuple(reversed(runners))
        for _name, runner in ordered:
            observation = runner(quality_rows, batch_size)
            if observation.row_count != row_count:
                raise RuntimeError(
                    "Quality transport benchmark inserted an incomplete row set"
                )
            observations.append(observation)
    return tuple(observations)


def run_source_accounting_benchmark(
    *,
    row_count: int = 4_000,
    batch_size: int = 1_000,
    rounds: int = 3,
) -> tuple[TransportObservation, ...]:
    """Run both transports for source entries and their row links."""

    if row_count < 2 or batch_size < 1 or rounds < 1:
        raise ValueError(
            "Source benchmark needs two rows and positive batch/round counts"
        )
    entries = tuple(
        SyntheticSourceAccounting(index)
        for index in range(row_count)
    )
    runners: tuple[
        tuple[
            str,
            Callable[
                [Sequence[SyntheticSourceAccounting], int],
                TransportObservation,
            ],
        ],
        ...,
    ] = (
        ("column_arrays", _source_accounting_column_arrays),
        ("typed_json", _source_accounting_typed_json),
    )
    observations: list[TransportObservation] = []
    for round_index in range(rounds):
        ordered = runners if round_index % 2 == 0 else tuple(reversed(runners))
        for _name, runner in ordered:
            observation = runner(entries, batch_size)
            if observation.row_count != row_count:
                raise RuntimeError(
                    "Source benchmark inserted an incomplete entry set"
                )
            observations.append(observation)
    return tuple(observations)


def run_preparation_impact_benchmark(
    *,
    row_count: int = 15_873,
    batch_size: int = 5_000,
    rounds: int = 3,
) -> tuple[TransportObservation, ...]:
    """Run current and typed transports for preparation impacts."""

    if row_count < 1 or batch_size < 1 or rounds < 1:
        raise ValueError("Benchmark rows, batch size, and rounds must be positive")
    impacts = tuple(
        SyntheticPreparationImpact(index)
        for index in range(row_count)
    )
    runners: tuple[
        tuple[
            str,
            Callable[
                [Sequence[SyntheticPreparationImpact], int],
                TransportObservation,
            ],
        ],
        ...,
    ] = (
        ("column_arrays", _impact_column_arrays),
        ("typed_json", _impact_typed_json),
    )
    observations: list[TransportObservation] = []
    for round_index in range(rounds):
        ordered = runners if round_index % 2 == 0 else tuple(reversed(runners))
        for _name, runner in ordered:
            observation = runner(impacts, batch_size)
            if observation.row_count != row_count:
                raise RuntimeError(
                    "Impact benchmark inserted an incomplete row set"
                )
            observations.append(observation)
    return tuple(observations)


def run_canonical_row_benchmark(
    *,
    row_count: int = 4_000,
    batch_size: int = 5_000,
    rounds: int = 3,
) -> tuple[TransportObservation, ...]:
    """Run current and typed transports for direct canonical rows."""

    if row_count < 1 or batch_size < 1 or rounds < 1:
        raise ValueError("Benchmark rows, batch size, and rounds must be positive")
    canonical_rows = tuple(
        SyntheticCanonicalRow(index)
        for index in range(row_count)
    )
    runners: tuple[
        tuple[
            str,
            Callable[
                [Sequence[SyntheticCanonicalRow], int],
                TransportObservation,
            ],
        ],
        ...,
    ] = (
        ("column_arrays", _canonical_column_arrays),
        ("typed_json", _canonical_typed_json),
    )
    observations: list[TransportObservation] = []
    for round_index in range(rounds):
        ordered = runners if round_index % 2 == 0 else tuple(reversed(runners))
        for _name, runner in ordered:
            observation = runner(canonical_rows, batch_size)
            if observation.row_count != row_count:
                raise RuntimeError(
                    "Canonical benchmark inserted an incomplete row set"
                )
            observations.append(observation)
    return tuple(observations)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--family",
        choices=(
            "normalization-effects",
            "quality-row-results",
            "source-accounting",
            "preparation-impacts",
            "canonical-rows",
        ),
        default="normalization-effects",
    )
    parser.add_argument("--rows", type=int)
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--rounds", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.family == "quality-row-results":
        runner = run_quality_row_benchmark
    elif arguments.family == "source-accounting":
        runner = run_source_accounting_benchmark
    elif arguments.family == "preparation-impacts":
        runner = run_preparation_impact_benchmark
    elif arguments.family == "canonical-rows":
        runner = run_canonical_row_benchmark
    else:
        runner = run_benchmark
    row_count = arguments.rows
    if row_count is None:
        row_count = (
            15_873
            if arguments.family in {
                "normalization-effects",
                "preparation-impacts",
            }
            else 4_000
        )
    observations = runner(
        row_count=row_count,
        batch_size=arguments.batch_size,
        rounds=arguments.rounds,
    )
    for observation in observations:
        print(
            f"family={arguments.family} "
            f"transport={observation.transport} "
            f"rows={observation.row_count} "
            f"batches={observation.batch_count} "
            f"related_rows={observation.related_row_count} "
            f"transport_bytes={observation.transport_bytes} "
            f"elapsed={observation.elapsed_seconds:.6f}s"
        )
    for transport in ("column_arrays", "typed_json"):
        samples = [
            item.elapsed_seconds
            for item in observations
            if item.transport == transport
        ]
        print(
            f"median family={arguments.family} "
            f"transport={transport} rounds={len(samples)} "
            f"elapsed={median(samples):.6f}s"
        )


if __name__ == "__main__":
    main()
