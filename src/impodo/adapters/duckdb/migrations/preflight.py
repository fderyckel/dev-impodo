"""Schema evolution for durable, protected preflight evidence."""

from __future__ import annotations

import duckdb


def create_preflight_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Add Slice 5 evidence without promoting historical readiness rows."""

    for name, definition in (
        ("normalization_run_id", "VARCHAR DEFAULT ''"),
        ("normalization_content_hash", "VARCHAR DEFAULT ''"),
        ("normalization_lifecycle_version", "BIGINT DEFAULT 0"),
        ("eligible_dataset_hash", "VARCHAR DEFAULT ''"),
        ("frozen_input_hash", "VARCHAR DEFAULT ''"),
        ("requirement_plan_hash", "VARCHAR DEFAULT ''"),
        ("metadata_snapshot_hash", "VARCHAR DEFAULT ''"),
        ("record_snapshot_hash", "VARCHAR DEFAULT ''"),
        ("result_hash", "VARCHAR DEFAULT ''"),
        ("manifest_hash", "VARCHAR DEFAULT ''"),
    ):
        connection.execute(
            f"ALTER TABLE readiness_run ADD COLUMN IF NOT EXISTS {name} {definition}"
        )
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
    connection.execute(
        """
        ALTER TABLE preflight_decision
        ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT ''
        """
    )
