"""Create portable Many2one supporting-lookup snapshot tables."""

from __future__ import annotations

import duckdb


def create_supporting_lookup_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create immutable revisions and their replaceable current pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS supporting_lookup_revision (
            snapshot_id VARCHAR PRIMARY KEY,
            lookup_key VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            relation_model VARCHAR NOT NULL,
            captured_at VARCHAR NOT NULL,
            snapshot_json VARCHAR NOT NULL
        );

        CREATE INDEX IF NOT EXISTS supporting_lookup_revision_lookup_key
            ON supporting_lookup_revision (lookup_key);

        CREATE TABLE IF NOT EXISTS supporting_lookup_current (
            lookup_key VARCHAR PRIMARY KEY,
            snapshot_id VARCHAR NOT NULL
        );
        """
    )
