"""Additive Recipe/DataVersion linkage tables for contained workspaces."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb


RECIPE_WORKSPACE_MIGRATION_ID = "2026-08-19-recipe-workspace-linkage-v1"
RECIPE_WORKSPACE_MIGRATION_CHECKSUM = (
    "sha256:69db2bfa07598d1ae51b1f10250f6ca637ad94f1dd45580fe027c602cb0e182f"
)
RECIPE_APPLICATION_MIGRATION_ID = "2026-08-19-recipe-application-v1"
RECIPE_APPLICATION_MIGRATION_CHECKSUM = (
    "sha256:a1f6dc55781b208a58d8b1fa0cc209735288c81b382cf26870b3ee467376b2a4"
)
RECIPE_QUALITY_SEED_MIGRATION_ID = "2026-08-19-recipe-quality-seed-v1"
RECIPE_QUALITY_SEED_MIGRATION_CHECKSUM = (
    "sha256:2f464c02d55821843147e55eae2a85732d83e992bdb61d53caa50a899b17ee93"
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
    _ensure_recipe_application_schema(connection)
    _ensure_recipe_quality_seed_schema(connection)


def _ensure_recipe_quality_seed_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Carry reusable business checks until the fresh mapping is confirmed."""

    connection.execute(
        """
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
    existing = connection.execute(
        """
        SELECT checksum
          FROM project_schema_migration
         WHERE migration_id = ?
        """,
        [RECIPE_QUALITY_SEED_MIGRATION_ID],
    ).fetchone()
    if (
        existing is not None
        and str(existing[0]) != RECIPE_QUALITY_SEED_MIGRATION_CHECKSUM
    ):
        raise RuntimeError("Recipe quality-seed migration checksum changed")
    connection.execute(
        """
        INSERT OR IGNORE INTO project_schema_migration
        VALUES (?, ?, ?)
        """,
        [
            RECIPE_QUALITY_SEED_MIGRATION_ID,
            RECIPE_QUALITY_SEED_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )


def _ensure_recipe_application_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Add exact R3 inputs without widening every existing evidence table."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_target_binding (
            target_binding_id VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL UNIQUE,
            binding_json VARCHAR NOT NULL,
            captured_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_target_binding_current (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            target_binding_id VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_parameter_values (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            content_hash VARCHAR NOT NULL,
            values_json VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_control_values (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            content_hash VARCHAR NOT NULL,
            values_json VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_application_evidence (
            application_id VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL,
            evidence_json VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL
        );
        """
    )
    existing = connection.execute(
        """
        SELECT checksum
          FROM project_schema_migration
         WHERE migration_id = ?
        """,
        [RECIPE_APPLICATION_MIGRATION_ID],
    ).fetchone()
    if (
        existing is not None
        and str(existing[0]) != RECIPE_APPLICATION_MIGRATION_CHECKSUM
    ):
        raise RuntimeError("Recipe application migration checksum changed")
    connection.execute(
        """
        INSERT OR IGNORE INTO project_schema_migration
        VALUES (?, ?, ?)
        """,
        [
            RECIPE_APPLICATION_MIGRATION_ID,
            RECIPE_APPLICATION_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )
