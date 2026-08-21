"""Additive schema for portable Many2one supporting-lookup snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb


SUPPORTING_LOOKUP_MIGRATION_ID = "2026-08-21-supporting-lookup-v1"
SUPPORTING_LOOKUP_MIGRATION_CHECKSUM = (
    "sha256:6c35a80ed12c68121da894fe1b56b71bbca4ff9c22e996cd617696844cf7b6da"
)


def ensure_supporting_lookup_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create immutable revisions and their replaceable current pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_schema_migration (
            migration_id VARCHAR PRIMARY KEY,
            checksum VARCHAR NOT NULL,
            applied_at VARCHAR NOT NULL
        );

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
    existing = connection.execute(
        """
        SELECT checksum
          FROM project_schema_migration
         WHERE migration_id = ?
        """,
        [SUPPORTING_LOOKUP_MIGRATION_ID],
    ).fetchone()
    if (
        existing is not None
        and str(existing[0]) != SUPPORTING_LOOKUP_MIGRATION_CHECKSUM
    ):
        raise RuntimeError("Supporting lookup migration checksum changed")
    connection.execute(
        """
        INSERT OR IGNORE INTO project_schema_migration
        VALUES (?, ?, ?)
        """,
        [
            SUPPORTING_LOOKUP_MIGRATION_ID,
            SUPPORTING_LOOKUP_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )
