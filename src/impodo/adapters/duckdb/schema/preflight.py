"""DuckDB schema for durable, protected preflight evidence."""

from __future__ import annotations

import duckdb


def create_preflight_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create preflight evidence relations and current pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS preflight_current (
            singleton_id INTEGER PRIMARY KEY,
            run_id VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS preflight_dataset (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            dataset VARCHAR NOT NULL,
            summary_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal)
        );

        CREATE INDEX IF NOT EXISTS preflight_dataset_lookup
            ON preflight_dataset (run_id, dataset);

        CREATE TABLE IF NOT EXISTS preflight_decision (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            source_trace_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            status VARCHAR NOT NULL,
            decision_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal),
            UNIQUE (run_id, source_trace_id)
        );

        CREATE INDEX IF NOT EXISTS preflight_decision_lookup
            ON preflight_decision (run_id, dataset, source_row);

        CREATE TABLE IF NOT EXISTS preflight_target_snapshot (
            run_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            snapshot_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, kind)
        );

        CREATE TABLE IF NOT EXISTS preflight_transition (
            run_id VARCHAR NOT NULL,
            event_type VARCHAR NOT NULL,
            occurred_at VARCHAR NOT NULL,
            actor VARCHAR NOT NULL,
            detail VARCHAR NOT NULL,
            PRIMARY KEY (run_id, event_type)
        );
        """
    )
