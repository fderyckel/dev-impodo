"""Schema setup for the small cross-project registry database."""

from __future__ import annotations

import duckdb


def ensure_registry_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create registry projections and their bounded recovery journal."""

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

