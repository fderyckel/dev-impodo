"""DuckDB lifecycle schema for immutable mapping-bound prepared snapshots."""

from __future__ import annotations

import duckdb


def create_prepared_snapshot_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create historical manifests, session bindings, and current pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS prepared_snapshot_manifest (
            content_hash VARCHAR PRIMARY KEY,
            dataset_id VARCHAR NOT NULL,
            logical_hash VARCHAR NOT NULL,
            source_snapshot_hash VARCHAR NOT NULL,
            mapping_hash VARCHAR NOT NULL,
            schema_hash VARCHAR NOT NULL,
            transformation_program_hash VARCHAR NOT NULL,
            row_count BIGINT NOT NULL,
            parquet_sha256 VARCHAR NOT NULL,
            parquet_storage_key VARCHAR NOT NULL UNIQUE,
            created_at VARCHAR NOT NULL,
            manifest_json VARCHAR NOT NULL,
            UNIQUE (dataset_id, logical_hash, parquet_sha256)
        );

        CREATE INDEX IF NOT EXISTS prepared_snapshot_manifest_lookup
            ON prepared_snapshot_manifest (dataset_id, logical_hash);

        CREATE TABLE IF NOT EXISTS preparation_session_snapshot (
            session_id VARCHAR NOT NULL,
            dataset_id VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            PRIMARY KEY (session_id, dataset_id)
        );

        CREATE TABLE IF NOT EXISTS prepared_snapshot_current (
            dataset_id VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL
        );
        """
    )
