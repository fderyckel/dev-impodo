"""Workspace data-plane storage for immutable business expectations."""

import duckdb

BUSINESS_INTENT_COLUMNS = (
    "version",
    "operation_id",
    "request_hash",
    "content_hash",
    "revision_json",
)


def create_business_intent_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create no approved default or current comparison during migration."""
    connection.execute("""
        CREATE TABLE business_intent_revision (
            version INTEGER PRIMARY KEY,
            operation_id VARCHAR NOT NULL UNIQUE,
            request_hash VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            revision_json VARCHAR NOT NULL
        )
    """)
