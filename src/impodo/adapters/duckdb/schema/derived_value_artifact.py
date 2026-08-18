"""DuckDB lifecycle schema for immutable derived/grouped value artifacts."""

from __future__ import annotations

import duckdb


def create_derived_value_artifact_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create historical manifests, pending-session bindings, and pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS derived_value_artifact_manifest (
            content_hash VARCHAR PRIMARY KEY,
            dataset_id VARCHAR NOT NULL,
            logical_hash VARCHAR NOT NULL,
            derivation_kind VARCHAR NOT NULL,
            physical_selection_hash VARCHAR NOT NULL,
            source_selection_hash VARCHAR NOT NULL,
            derived_plan_hash VARCHAR NOT NULL,
            derivation_rule_hash VARCHAR NOT NULL,
            mapping_hash VARCHAR NOT NULL,
            schema_hash VARCHAR NOT NULL,
            transformation_program_hash VARCHAR NOT NULL,
            lineage_hash VARCHAR NOT NULL,
            writer_contract_version INTEGER NOT NULL,
            row_count BIGINT NOT NULL,
            physical_schema_hash VARCHAR NOT NULL,
            parquet_sha256 VARCHAR NOT NULL,
            parquet_storage_key VARCHAR NOT NULL UNIQUE,
            created_at VARCHAR NOT NULL,
            manifest_json VARCHAR NOT NULL,
            UNIQUE (dataset_id, logical_hash, parquet_sha256)
        );

        CREATE INDEX IF NOT EXISTS derived_value_artifact_manifest_lookup
            ON derived_value_artifact_manifest (dataset_id, logical_hash);

        CREATE TABLE IF NOT EXISTS preparation_session_derived_artifact (
            session_id VARCHAR NOT NULL,
            dataset_id VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            PRIMARY KEY (session_id, dataset_id)
        );

        CREATE TABLE IF NOT EXISTS derived_value_artifact_current (
            dataset_id VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL
        );
        """
    )
