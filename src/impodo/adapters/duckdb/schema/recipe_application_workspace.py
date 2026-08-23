"""Create the Recipe compilation tables used by one current workspace."""

from __future__ import annotations

import duckdb


def create_recipe_application_workspace_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create current tables without migrating Recipe-first workspace state."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_parameter_definitions (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            content_hash VARCHAR NOT NULL,
            definitions_json VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_quality_seed (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            application_id VARCHAR NOT NULL,
            mapping_content_hash VARCHAR NOT NULL,
            rules_json VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL
        );
        """
    )
