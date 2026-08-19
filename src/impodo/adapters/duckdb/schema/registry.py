"""Schema setup and migrations for the cross-project registry database."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

import duckdb


RECIPE_REGISTRY_MIGRATION_ID = "2026-08-19-recipe-root-v1"
RECIPE_REGISTRY_MIGRATION_CHECKSUM = (
    "sha256:ee5f62e9ff7400a62e65195dd84759398c60cf831ea178909bd88c93b4151bd9"
)


def ensure_registry_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create registry projections, migration ledger, and Recipe lineage."""

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
    _ensure_recipe_registry_schema(connection)
    _backfill_project_recipes(connection)


def ensure_project_recipe_shell(
    connection: duckdb.DuckDBPyConnection,
    *,
    project_id: str,
    name: str,
    project_status: str,
    updated_at: str,
) -> tuple[str, str]:
    """Ensure one legacy-compatible Recipe/DataVersion shell for a project.

    This operates only on the lightweight registry connection. It never opens
    the contained project database and therefore remains safe for project-list
    and startup backfill paths.
    """

    existing = connection.execute(
        """
        SELECT recipe_id, data_version_id
          FROM data_version
         WHERE workspace_project_id = ?
        """,
        [project_id],
    ).fetchone()
    if existing is not None:
        return str(existing[0]), str(existing[1])

    recipe_id = str(uuid4())
    data_version_id = str(uuid4())
    data_version_state = "ACTIVE" if project_status != "CLOSED" else "SEALED"
    connection.execute(
        """
        INSERT INTO recipe (
            recipe_id, display_name, business_purpose, state,
            data_classification, retention_days, current_recipe_revision,
            current_data_version_id, pending_data_version_id,
            cutover_candidate_id, setup_hydration_state,
            setup_hydration_hash, optimistic_revision, created_at, updated_at
        ) VALUES (?, ?, ?, 'ACTIVE', 'INTERNAL', 90, NULL, ?, NULL, NULL,
                  'PENDING', NULL, 1, ?, ?)
        """,
        [
            recipe_id,
            name,
            "Legacy project awaiting Recipe publication",
            data_version_id,
            updated_at,
            updated_at,
        ],
    )
    connection.execute(
        """
        INSERT INTO data_version (
            data_version_id, recipe_id, version_number,
            workspace_project_id, parent_data_version_id, purpose, state,
            pinned_recipe_revision, label, export_as_of_date,
            parameter_values_hash, intake_status, created_at, sealed_at
        ) VALUES (?, ?, 1, ?, NULL, 'AUTHORING', ?, NULL, ?, NULL, NULL,
                  'LEGACY_BACKFILL', ?, NULL)
        """,
        [
            data_version_id,
            recipe_id,
            project_id,
            data_version_state,
            f"{name} data version 1",
            updated_at,
        ],
    )
    return recipe_id, data_version_id


def _ensure_recipe_registry_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS registry_schema_migration (
            migration_id VARCHAR PRIMARY KEY,
            checksum VARCHAR NOT NULL,
            applied_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe (
            recipe_id VARCHAR PRIMARY KEY,
            display_name VARCHAR NOT NULL,
            business_purpose VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            data_classification VARCHAR NOT NULL,
            retention_days INTEGER NOT NULL,
            current_recipe_revision INTEGER,
            current_data_version_id VARCHAR,
            pending_data_version_id VARCHAR,
            cutover_candidate_id VARCHAR,
            setup_hydration_state VARCHAR NOT NULL,
            setup_hydration_hash VARCHAR,
            optimistic_revision INTEGER NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_revision (
            recipe_id VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            parent_version INTEGER,
            semantic_hash VARCHAR NOT NULL,
            payload_hash VARCHAR NOT NULL,
            storage_key VARCHAR NOT NULL,
            artifact_hash VARCHAR NOT NULL,
            size_bytes BIGINT NOT NULL,
            contract_versions_json VARCHAR NOT NULL,
            provenance_json VARCHAR NOT NULL,
            actor_issuer VARCHAR NOT NULL,
            actor_subject VARCHAR NOT NULL,
            actor_display_name VARCHAR NOT NULL,
            published_at VARCHAR NOT NULL,
            PRIMARY KEY (recipe_id, version),
            UNIQUE (recipe_id, semantic_hash)
        );

        CREATE TABLE IF NOT EXISTS data_version (
            data_version_id VARCHAR PRIMARY KEY,
            recipe_id VARCHAR NOT NULL,
            version_number INTEGER NOT NULL,
            workspace_project_id VARCHAR NOT NULL UNIQUE,
            parent_data_version_id VARCHAR,
            purpose VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            pinned_recipe_revision INTEGER,
            label VARCHAR NOT NULL,
            export_as_of_date VARCHAR,
            parameter_values_hash VARCHAR,
            intake_status VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            sealed_at VARCHAR,
            UNIQUE (recipe_id, version_number)
        );

        CREATE TABLE IF NOT EXISTS recipe_application (
            application_id VARCHAR PRIMARY KEY,
            recipe_id VARCHAR NOT NULL,
            recipe_revision INTEGER NOT NULL,
            data_version_id VARCHAR NOT NULL,
            workspace_project_id VARCHAR NOT NULL,
            source_selection_hash VARCHAR NOT NULL,
            parameter_values_hash VARCHAR NOT NULL,
            target_binding_hash VARCHAR NOT NULL,
            credential_generation VARCHAR NOT NULL,
            binding_hash VARCHAR NOT NULL,
            issue_hash VARCHAR NOT NULL,
            mapping_id VARCHAR,
            mapping_content_hash VARCHAR,
            status VARCHAR NOT NULL,
            evidence_storage_key VARCHAR NOT NULL,
            evidence_hash VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_qualification (
            qualification_id VARCHAR PRIMARY KEY,
            recipe_id VARCHAR NOT NULL,
            recipe_revision INTEGER NOT NULL,
            application_id VARCHAR NOT NULL,
            test_target_binding_hash VARCHAR NOT NULL,
            preparation_hash VARCHAR NOT NULL,
            quality_hash VARCHAR NOT NULL,
            control_hash VARCHAR NOT NULL,
            comparison_hash VARCHAR NOT NULL,
            execution_hash VARCHAR NOT NULL,
            read_back_hash VARCHAR NOT NULL,
            reconciliation_hash VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            findings_json VARCHAR NOT NULL,
            actor_issuer VARCHAR NOT NULL,
            actor_subject VARCHAR NOT NULL,
            actor_display_name VARCHAR NOT NULL,
            qualified_at VARCHAR NOT NULL,
            evidence_storage_key VARCHAR NOT NULL,
            evidence_hash VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cutover_candidate (
            cutover_candidate_id VARCHAR PRIMARY KEY,
            recipe_id VARCHAR NOT NULL UNIQUE,
            recipe_revision INTEGER NOT NULL,
            qualification_id VARCHAR NOT NULL,
            expected_recipe_revision INTEGER NOT NULL,
            actor_issuer VARCHAR NOT NULL,
            actor_subject VARCHAR NOT NULL,
            actor_display_name VARCHAR NOT NULL,
            selected_at VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_intent (
            operation_id VARCHAR PRIMARY KEY,
            recipe_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            expected_recipe_revision INTEGER NOT NULL,
            retry_count INTEGER NOT NULL,
            detail_json VARCHAR NOT NULL,
            last_error VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_deletion_target (
            operation_id VARCHAR NOT NULL,
            target_kind VARCHAR NOT NULL,
            target_id VARCHAR NOT NULL,
            deleted_at VARCHAR,
            PRIMARY KEY (operation_id, target_kind, target_id)
        );
        """
    )
    existing = connection.execute(
        """
        SELECT checksum
          FROM registry_schema_migration
         WHERE migration_id = ?
        """,
        [RECIPE_REGISTRY_MIGRATION_ID],
    ).fetchone()
    if existing is not None and str(existing[0]) != RECIPE_REGISTRY_MIGRATION_CHECKSUM:
        raise RuntimeError("Recipe registry migration checksum changed")
    connection.execute(
        """
        INSERT OR IGNORE INTO registry_schema_migration
        VALUES (?, ?, ?)
        """,
        [
            RECIPE_REGISTRY_MIGRATION_ID,
            RECIPE_REGISTRY_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )


def _backfill_project_recipes(connection: duckdb.DuckDBPyConnection) -> None:
    rows = connection.execute(
        """
        SELECT project_id, name, status, updated_at
          FROM project_registry
         WHERE project_id NOT IN (
               SELECT workspace_project_id FROM data_version
         )
         ORDER BY project_id
        """
    ).fetchall()
    for project_id, name, status, updated_at in rows:
        ensure_project_recipe_shell(
            connection,
            project_id=str(project_id),
            name=str(name),
            project_status=str(status),
            updated_at=str(updated_at),
        )


def recipe_migration_ledger_json(connection: duckdb.DuckDBPyConnection) -> str:
    """Return a deterministic bounded migration projection for diagnostics."""

    rows = connection.execute(
        """
        SELECT migration_id, checksum, applied_at
          FROM registry_schema_migration
         ORDER BY migration_id
        """
    ).fetchall()
    return json.dumps(
        [
            {
                "migration_id": str(row[0]),
                "checksum": str(row[1]),
                "applied_at": str(row[2]),
            }
            for row in rows
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
