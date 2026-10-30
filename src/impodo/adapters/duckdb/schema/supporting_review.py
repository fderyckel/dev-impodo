"""Storage shape of immutable supporting-data choice revisions."""

import duckdb

SUPPORTING_REVIEW_COLUMNS = ("version", "operation_id", "request_hash", "review_json")
SUPPORTING_REVIEW_SUMMARY_COLUMNS = ("singleton_id", "summary_json")


def create_supporting_review_summary_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Add a rebuildable projection without rewriting historical decisions."""
    connection.execute("""
        CREATE TABLE supporting_review_projection (
            singleton_id BOOLEAN PRIMARY KEY CHECK (singleton_id),
            summary_json VARCHAR NOT NULL
        )
    """)


def create_supporting_review_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the revision store without moving detailed decisions to PostgreSQL."""
    connection.execute("""
        CREATE TABLE supporting_data_review (
            version INTEGER PRIMARY KEY,
            operation_id VARCHAR NOT NULL UNIQUE,
            request_hash VARCHAR NOT NULL,
            review_json VARCHAR NOT NULL
        )
    """)
