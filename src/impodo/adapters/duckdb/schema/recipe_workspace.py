"""Additive Recipe/DataVersion linkage tables for contained workspaces."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb


RECIPE_WORKSPACE_MIGRATION_ID = "2026-08-19-recipe-workspace-linkage-v1"
RECIPE_WORKSPACE_MIGRATION_CHECKSUM = (
    "sha256:69db2bfa07598d1ae51b1f10250f6ca637ad94f1dd45580fe027c602cb0e182f"
)


def ensure_recipe_workspace_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Apply the bounded additive workspace migration once."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_schema_migration (
            migration_id VARCHAR PRIMARY KEY,
            checksum VARCHAR NOT NULL,
            applied_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_workspace_linkage (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            recipe_id VARCHAR NOT NULL,
            data_version_id VARCHAR NOT NULL,
            data_version_number INTEGER NOT NULL,
            linked_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_workspace_seal (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            sealed_at VARCHAR NOT NULL,
            reason VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_application_draft (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            application_id VARCHAR NOT NULL,
            recipe_id VARCHAR NOT NULL,
            recipe_revision INTEGER NOT NULL,
            data_version_id VARCHAR NOT NULL,
            target_binding_hash VARCHAR NOT NULL,
            source_selection_hash VARCHAR NOT NULL,
            parameter_values_hash VARCHAR NOT NULL,
            revision INTEGER NOT NULL,
            state VARCHAR NOT NULL,
            overrides_json VARCHAR NOT NULL,
            issue_fingerprints_json VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL,
            updated_by VARCHAR NOT NULL
        );
        """
    )
    existing = connection.execute(
        """
        SELECT checksum
          FROM project_schema_migration
         WHERE migration_id = ?
        """,
        [RECIPE_WORKSPACE_MIGRATION_ID],
    ).fetchone()
    if existing is not None and str(existing[0]) != RECIPE_WORKSPACE_MIGRATION_CHECKSUM:
        raise RuntimeError("Recipe workspace migration checksum changed")
    connection.execute(
        """
        INSERT OR IGNORE INTO project_schema_migration
        VALUES (?, ?, ?)
        """,
        [
            RECIPE_WORKSPACE_MIGRATION_ID,
            RECIPE_WORKSPACE_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )
