"""Schema setup for the small cross-project registry database."""

from __future__ import annotations

import duckdb


def ensure_registry_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create registry projections and cross-project lifecycle receipts."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_registry (
            project_id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            revision INTEGER NOT NULL,
            updated_at VARCHAR NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_registry_sync_pending (
            project_id VARCHAR PRIMARY KEY
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS credential_removal_receipt (
            receipt_hash VARCHAR PRIMARY KEY,
            project_id VARCHAR NOT NULL,
            credential_role VARCHAR NOT NULL,
            removal_reason VARCHAR NOT NULL,
            connection_target_hash VARCHAR NOT NULL,
            credential_binding_hash VARCHAR,
            storage_class VARCHAR NOT NULL,
            removed_at VARCHAR NOT NULL,
            actor_issuer VARCHAR NOT NULL,
            actor_subject VARCHAR NOT NULL,
            actor_display_name VARCHAR NOT NULL
        )
        """
    )
