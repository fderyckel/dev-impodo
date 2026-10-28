"""DuckDB receipt and fencing schema for shared-server operations."""

from __future__ import annotations

import duckdb

CROSS_STORE_DATA_RECEIPT_COLUMNS = (
    "receipt_id",
    "operation_id",
    "organization_id",
    "project_id",
    "workspace_id",
    "operation_kind",
    "semantic_input_hash",
    "fencing_token",
    "evidence_hash",
    "artifacts_json",
    "receipt_hash",
    "committed_at",
)
WORKSPACE_FENCE_COLUMNS = (
    "workspace_id",
    "organization_id",
    "project_id",
    "fencing_token",
    "operation_id",
    "updated_at",
)


def create_cross_store_coordination_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create append-only data receipts and the current workspace fence."""

    connection.execute(
        """
        CREATE TABLE cross_store_data_receipt (
            receipt_id VARCHAR PRIMARY KEY,
            operation_id VARCHAR NOT NULL UNIQUE,
            organization_id VARCHAR NOT NULL,
            project_id VARCHAR NOT NULL,
            workspace_id VARCHAR NOT NULL,
            operation_kind VARCHAR NOT NULL,
            semantic_input_hash VARCHAR NOT NULL,
            fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
            evidence_hash VARCHAR NOT NULL,
            artifacts_json VARCHAR NOT NULL,
            receipt_hash VARCHAR NOT NULL UNIQUE,
            committed_at VARCHAR NOT NULL
        );

        CREATE TABLE workspace_fence (
            workspace_id VARCHAR PRIMARY KEY,
            organization_id VARCHAR NOT NULL,
            project_id VARCHAR NOT NULL,
            fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
            operation_id VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL
        );
        """
    )


__all__ = (
    "CROSS_STORE_DATA_RECEIPT_COLUMNS",
    "WORKSPACE_FENCE_COLUMNS",
    "create_cross_store_coordination_schema",
)
