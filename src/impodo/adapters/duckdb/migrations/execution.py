"""DuckDB schema for practical Stage-J execution journals."""

from __future__ import annotations

import duckdb


def create_execution_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_run (
            run_id VARCHAR PRIMARY KEY,
            snapshot_hash VARCHAR NOT NULL,
            snapshot_root_hash VARCHAR NOT NULL,
            preflight_run_id VARCHAR NOT NULL,
            target_hash VARCHAR NOT NULL,
            target_database VARCHAR NOT NULL,
            batch_rows INTEGER,
            status VARCHAR NOT NULL,
            started_at VARCHAR NOT NULL,
            started_by VARCHAR NOT NULL,
            completed_at VARCHAR
        );

        CREATE TABLE IF NOT EXISTS execution_row (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            row_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            target_model VARCHAR NOT NULL,
            operation VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            attempt INTEGER NOT NULL,
            odoo_id BIGINT,
            safe_error VARCHAR NOT NULL,
            row_json VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal),
            UNIQUE (run_id, row_id)
        );

        CREATE INDEX IF NOT EXISTS execution_row_status_lookup
            ON execution_row (run_id, status, ordinal);

        CREATE TABLE IF NOT EXISTS execution_current (
            singleton_id INTEGER PRIMARY KEY,
            run_id VARCHAR NOT NULL
        );
        """
    )
