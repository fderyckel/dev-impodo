"""DuckDB manifest and current-pointer schema for immutable source snapshots."""

from __future__ import annotations

import duckdb


def create_source_snapshot_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create historical manifests separately from replaceable dataset pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_snapshot_manifest (
            content_hash VARCHAR PRIMARY KEY,
            dataset_id VARCHAR NOT NULL,
            logical_hash VARCHAR NOT NULL,
            parquet_sha256 VARCHAR NOT NULL,
            parquet_storage_key VARCHAR NOT NULL UNIQUE,
            created_at VARCHAR NOT NULL,
            manifest_json VARCHAR NOT NULL,
            UNIQUE (dataset_id, logical_hash, parquet_sha256)
        );

        CREATE INDEX IF NOT EXISTS source_snapshot_manifest_lookup
            ON source_snapshot_manifest (dataset_id, logical_hash);

        CREATE TABLE IF NOT EXISTS source_snapshot_current (
            dataset_id VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL
        );
        """
    )
