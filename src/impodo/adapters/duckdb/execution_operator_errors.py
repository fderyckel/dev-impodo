"""Keep private rejection receipts in the same transaction as row outcomes."""

from __future__ import annotations

import json
from collections.abc import Sequence

import duckdb

from impodo.domain.execution.models import ExecutionRowAttempt
from impodo.domain.execution.operator_errors import (
    MAX_RECEIPT_BYTES,
    OperatorErrorDetail,
    OperatorErrorPersistenceError,
    OperatorErrorReceipt,
)

OPERATOR_ERROR_COLUMNS = (
    "run_id",
    "transport_batch",
    "snapshot_hash",
    "detail_hash",
    "detail_json",
)


def create_operator_error_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the current private companion without changing historical rows."""
    connection.execute("""
        CREATE TABLE execution_operator_error (
            run_id VARCHAR NOT NULL,
            transport_batch BIGINT NOT NULL,
            snapshot_hash VARCHAR NOT NULL,
            detail_hash VARCHAR NOT NULL,
            detail_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, transport_batch)
        )
    """)


def write_operator_error(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    snapshot_hash: str,
    rows: Sequence[ExecutionRowAttempt],
    detail: OperatorErrorDetail,
) -> None:
    """Store one bounded exact batch once; public row JSON remains value-free."""
    if (
        not rows
        or len({row.transport_batch for row in rows}) != 1
        or len(rows) != len(detail.submitted)
    ):
        raise OperatorErrorPersistenceError("Private Odoo error membership is invalid")
    receipt = OperatorErrorReceipt(
        snapshot_hash, tuple(row.row_id for row in rows), detail
    )
    try:
        connection.execute(
            "INSERT INTO execution_operator_error VALUES (?, ?, ?, ?, ?)",
            [
                run_id,
                rows[0].transport_batch,
                snapshot_hash,
                receipt.content_hash,
                receipt.to_json(),
            ],
        )
    except (duckdb.Error, ValueError, TypeError, UnicodeError) as error:
        raise OperatorErrorPersistenceError(
            "Private Odoo explanation could not be saved"
        ) from error


def read_operator_error(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    row_id: str,
) -> tuple[ExecutionRowAttempt, OperatorErrorReceipt | None] | None:
    """Resolve only the requested saved row's transport and verify its binding."""
    record = connection.execute(
        """
        SELECT e.row_json, d.detail_json, d.detail_hash, r.snapshot_hash
          FROM execution_row e
          JOIN execution_run r USING (run_id)
          LEFT JOIN execution_operator_error d ON d.run_id = e.run_id
           AND d.transport_batch = json_extract(e.row_json, '$.transport_batch')::BIGINT
         WHERE e.run_id = ? AND e.row_id = ?
    """,
        [run_id, row_id],
    ).fetchone()
    if record is None:
        return None
    row = ExecutionRowAttempt.from_json(str(record[0]))
    if record[1] is None:
        return row, None
    try:
        if len(str(record[1]).encode()) > MAX_RECEIPT_BYTES:
            return row, None
        payload = json.loads(str(record[1]))
        receipt = OperatorErrorReceipt(
            str(payload["snapshot_hash"]),
            tuple(payload["row_ids"]),
            OperatorErrorDetail.from_json(payload["detail"]),
        )
        if (
            receipt.content_hash != str(record[2])
            or receipt.snapshot_hash != str(record[3])
            or row_id not in receipt.row_ids
        ):
            return row, None
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return row, None
    return row, receipt
