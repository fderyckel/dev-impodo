"""Benchmark bounded transports for the integrated relationship-edge schema.

The synthetic values match the Phase-6 physical types and cardinality without
containing customer data. Every observation includes payload construction,
transport, insertion, and a row-count/state verification.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import statistics
from time import perf_counter
from typing import Callable

import duckdb

from impodo.adapters.duckdb.constants import DUCKDB_JSON_BATCH_MAX_BYTES
from impodo.adapters.duckdb.serialization import iter_encoded_json_batches
from impodo.adapters.duckdb.unit_of_work import DUCKDB_CONFIG


_STRUCTURE = """[{
    "child_ordinal":"BIGINT",
    "target_field":"VARCHAR",
    "item_ordinal":"INTEGER",
    "parent_dataset":"VARCHAR",
    "normalized_key_json":"VARCHAR",
    "parent_identity_hash":"VARCHAR"
}]"""
_FIELDS = (
    "child_ordinal",
    "target_field",
    "item_ordinal",
    "parent_dataset",
    "normalized_key_json",
    "parent_identity_hash",
)


@dataclass(frozen=True, slots=True)
class Observation:
    transport: str
    elapsed_seconds: float
    rows: int
    batches: int
    transport_bytes: int


def _row(index: int, product_count: int) -> dict[str, object]:
    key = f"P-{(index % product_count) + 1:06d}"
    return {
        "child_ordinal": index,
        "target_field": "product_id",
        "item_ordinal": 0,
        "parent_dataset": "products",
        "normalized_key_json": json.dumps([key], separators=(",", ":")),
        "parent_identity_hash": f"sha256:{index % product_count:064x}",
    }


def _connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:", config=dict(DUCKDB_CONFIG))
    connection.execute(
        """
        CREATE TABLE relationship_edge (
            session_id VARCHAR NOT NULL,
            child_ordinal BIGINT NOT NULL,
            target_field VARCHAR NOT NULL,
            item_ordinal INTEGER NOT NULL,
            parent_dataset VARCHAR NOT NULL,
            normalized_key_json VARCHAR NOT NULL,
            parent_identity_hash VARCHAR NOT NULL,
            match_state VARCHAR NOT NULL,
            resolution_state VARCHAR NOT NULL,
            match_count BIGINT NOT NULL,
            resolved_parent_ordinal BIGINT
        )
        """
    )
    return connection


def _verify(connection: duckdb.DuckDBPyConnection, expected: int) -> None:
    observed = connection.execute(
        """
        SELECT COUNT(*), COUNT(*) FILTER (
            WHERE match_state = 'PENDING'
              AND resolution_state = 'PENDING'
              AND match_count = 0
        )
          FROM relationship_edge
        """
    ).fetchone()
    if observed != (expected, expected):
        raise RuntimeError("Relationship transport changed the typed row set")


def _typed_json(rows: int, batch_size: int, products: int) -> Observation:
    connection = _connection()
    batches = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, rows, batch_size):
            values = (
                _row(index, products)
                for index in range(start, min(rows, start + batch_size))
            )
            for encoded in iter_encoded_json_batches(
                values,
                max_rows=batch_size,
                max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
            ):
                connection.execute(
                    """
                    INSERT INTO relationship_edge
                    SELECT 'benchmark', item.child_ordinal,
                           item.target_field, item.item_ordinal,
                           item.parent_dataset, item.normalized_key_json,
                           item.parent_identity_hash,
                           'PENDING', 'PENDING', 0, NULL
                      FROM (
                          SELECT UNNEST(
                              from_json_strict(CAST(? AS JSON), ?)
                          ) AS item
                      )
                    """,
                    [encoded.payload, _STRUCTURE],
                )
                batches += 1
                transport_bytes += encoded.byte_count
        elapsed = perf_counter() - started
        _verify(connection, rows)
    finally:
        connection.close()
    return Observation("typed_json", elapsed, rows, batches, transport_bytes)


def _column_arrays(rows: int, batch_size: int, products: int) -> Observation:
    connection = _connection()
    batches = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, rows, batch_size):
            items = [
                _row(index, products)
                for index in range(start, min(rows, start + batch_size))
            ]
            columns = [[item[field] for item in items] for field in _FIELDS]
            transport_bytes += sum(
                len(str(value).encode("utf-8"))
                for column in columns
                for value in column
            )
            connection.execute(
                """
                INSERT INTO relationship_edge
                SELECT 'benchmark', unnest(?), unnest(?), unnest(?),
                       unnest(?), unnest(?), unnest(?),
                       'PENDING', 'PENDING', 0, NULL
                """,
                columns,
            )
            batches += 1
        elapsed = perf_counter() - started
        _verify(connection, rows)
    finally:
        connection.close()
    return Observation("column_arrays", elapsed, rows, batches, transport_bytes)


def _parameterized(rows: int, batch_size: int, products: int) -> Observation:
    connection = _connection()
    batches = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, rows, batch_size):
            items = [
                _row(index, products)
                for index in range(start, min(rows, start + batch_size))
            ]
            parameters = [tuple(item[field] for field in _FIELDS) for item in items]
            transport_bytes += sum(
                len(str(value).encode("utf-8")) for item in parameters for value in item
            )
            connection.executemany(
                """
                INSERT INTO relationship_edge VALUES (
                    'benchmark', ?, ?, ?, ?, ?, ?,
                    'PENDING', 'PENDING', 0, NULL
                )
                """,
                parameters,
            )
            batches += 1
        elapsed = perf_counter() - started
        _verify(connection, rows)
    finally:
        connection.close()
    return Observation(
        "parameterized_executemany",
        elapsed,
        rows,
        batches,
        transport_bytes,
    )


def _polars_arrow(rows: int, batch_size: int, products: int) -> Observation:
    import polars as pl

    connection = _connection()
    batches = 0
    transport_bytes = 0
    started = perf_counter()
    try:
        for start in range(0, rows, batch_size):
            items = [
                _row(index, products)
                for index in range(start, min(rows, start + batch_size))
            ]
            frame = pl.DataFrame(items)
            transport_bytes += int(frame.estimated_size())
            connection.register("relationship_batch", frame)
            try:
                connection.execute(
                    """
                    INSERT INTO relationship_edge
                    SELECT 'benchmark', child_ordinal, target_field,
                           item_ordinal, parent_dataset,
                           normalized_key_json, parent_identity_hash,
                           'PENDING', 'PENDING', 0, NULL
                      FROM relationship_batch
                    """
                )
            finally:
                connection.unregister("relationship_batch")
            batches += 1
        elapsed = perf_counter() - started
        _verify(connection, rows)
    finally:
        connection.close()
    return Observation("polars_arrow", elapsed, rows, batches, transport_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=80_000)
    parser.add_argument("--products", type=int, default=16_000)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--include-arrow", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if (
        min(
            arguments.rows,
            arguments.products,
            arguments.batch_size,
            arguments.rounds,
        )
        < 1
    ):
        raise SystemExit("Benchmark sizes must be positive")
    runners: list[Callable[[int, int, int], Observation]] = [
        _typed_json,
        _column_arrays,
        _parameterized,
    ]
    if arguments.include_arrow:
        runners.append(_polars_arrow)
    observations: list[Observation] = []
    for round_index in range(arguments.rounds):
        order = runners if round_index % 2 == 0 else list(reversed(runners))
        for runner in order:
            observations.append(
                runner(
                    arguments.rows,
                    arguments.batch_size,
                    arguments.products,
                )
            )
    medians = {}
    for transport in sorted({item.transport for item in observations}):
        samples = [
            item.elapsed_seconds for item in observations if item.transport == transport
        ]
        medians[transport] = statistics.median(samples)
    report = {
        "batch_size": arguments.batch_size,
        "include_arrow": arguments.include_arrow,
        "medians": medians,
        "observations": [asdict(item) for item in observations],
        "products": arguments.products,
        "result_schema_version": 1,
        "rounds": arguments.rounds,
        "rows": arguments.rows,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
