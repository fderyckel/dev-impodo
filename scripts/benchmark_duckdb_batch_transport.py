"""Compare bounded Python-to-DuckDB transports with synthetic effect rows.

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

from impodo.adapters.duckdb.constants import DUCKDB_JSON_BATCH_MAX_BYTES
from impodo.adapters.duckdb.normalization_repository import (
    _NORMALIZATION_EFFECT_JSON_STRUCTURE,
)
from impodo.adapters.duckdb.quality_repository import (
    _QUALITY_ROW_RESULT_JSON_STRUCTURE,
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
class TransportObservation:
    """Non-sensitive result of one transport benchmark round."""

    transport: str
    row_count: int
    batch_count: int
    transport_bytes: int
    elapsed_seconds: float


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
                field: row[field]
                for field in _QUALITY_ROW_FIELDS
                if field != "run_id"
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--family",
        choices=("normalization-effects", "quality-row-results"),
        default="normalization-effects",
    )
    parser.add_argument("--rows", type=int, default=15_873)
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--rounds", type=int, default=3)
    arguments = parser.parse_args()
    runner = (
        run_quality_row_benchmark
        if arguments.family == "quality-row-results"
        else run_benchmark
    )
    observations = runner(
        row_count=arguments.rows,
        batch_size=arguments.batch_size,
        rounds=arguments.rounds,
    )
    for observation in observations:
        print(
            f"transport={observation.transport} "
            f"rows={observation.row_count} "
            f"batches={observation.batch_count} "
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
            f"median transport={transport} rounds={len(samples)} "
            f"elapsed={median(samples):.6f}s"
        )


if __name__ == "__main__":
    main()
