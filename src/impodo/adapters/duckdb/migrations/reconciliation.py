"""DuckDB schema for practical Stage-K read-back results."""

from __future__ import annotations

import duckdb


def create_reconciliation_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_run (
            reconciliation_id VARCHAR PRIMARY KEY,
            execution_run_id VARCHAR UNIQUE NOT NULL,
            snapshot_hash VARCHAR NOT NULL,
            target_hash VARCHAR NOT NULL,
            target_database VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            verified_at VARCHAR NOT NULL,
            verified_by VARCHAR NOT NULL,
            report_hash VARCHAR NOT NULL,
            report_json VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reconciliation_current (
            singleton_id INTEGER PRIMARY KEY,
            reconciliation_id VARCHAR NOT NULL
        );
        """
    )
