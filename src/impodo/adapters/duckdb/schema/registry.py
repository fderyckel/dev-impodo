"""Schema setup and migrations for the cross-project registry database."""

from __future__ import annotations

from datetime import datetime, timezone
import duckdb


RECIPE_REGISTRY_MIGRATION_ID = "2026-08-19-recipe-root-v1"
RECIPE_REGISTRY_MIGRATION_CHECKSUM = (
    "sha256:ee5f62e9ff7400a62e65195dd84759398c60cf831ea178909bd88c93b4151bd9"
)
RECIPE_CLEAN_ROOT_MIGRATION_ID = "2026-08-19-recipe-clean-root-v2"
RECIPE_CLEAN_ROOT_MIGRATION_CHECKSUM = (
    "sha256:84954535ac8c1342ca4735553811a24c9347e13b53ad38ce9434224c83049e89"
)
RECIPE_CREATION_IDEMPOTENCY_MIGRATION_ID = (
    "2026-08-19-recipe-creation-idempotency-v3"
)
RECIPE_CREATION_IDEMPOTENCY_MIGRATION_CHECKSUM = (
    "sha256:fa615fef0b79872f6544419e21a4e528c1445a07d98144ba6e5b7b34b32a97e4"
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
            project_id VARCHAR PRIMARY KEY,
            recipe_id VARCHAR,
            data_version_id VARCHAR
        )
        """
    )
    connection.execute(
        "ALTER TABLE project_registry_sync_pending "
        "ADD COLUMN IF NOT EXISTS recipe_id VARCHAR"
    )
    connection.execute(
        "ALTER TABLE project_registry_sync_pending "
        "ADD COLUMN IF NOT EXISTS data_version_id VARCHAR"
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
            data_classification VARCHAR NOT NULL,
            retention_days INTEGER NOT NULL,
            current_recipe_revision INTEGER,
            current_data_version_id VARCHAR,
            cutover_candidate_id VARCHAR,
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
            recipe_id VARCHAR NOT NULL,
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
            detail_json VARCHAR NOT NULL,
            last_error VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL
        );

        """
    )
    _migrate_clean_recipe_root(connection)
    _migrate_recipe_creation_idempotency(connection)
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
    clean_existing = connection.execute(
        "SELECT checksum FROM registry_schema_migration WHERE migration_id = ?",
        [RECIPE_CLEAN_ROOT_MIGRATION_ID],
    ).fetchone()
    if (
        clean_existing is not None
        and str(clean_existing[0]) != RECIPE_CLEAN_ROOT_MIGRATION_CHECKSUM
    ):
        raise RuntimeError("Clean Recipe-root migration checksum changed")
    connection.execute(
        "INSERT OR IGNORE INTO registry_schema_migration VALUES (?, ?, ?)",
        [
            RECIPE_CLEAN_ROOT_MIGRATION_ID,
            RECIPE_CLEAN_ROOT_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )
    creation_existing = connection.execute(
        "SELECT checksum FROM registry_schema_migration WHERE migration_id = ?",
        [RECIPE_CREATION_IDEMPOTENCY_MIGRATION_ID],
    ).fetchone()
    if (
        creation_existing is not None
        and str(creation_existing[0])
        != RECIPE_CREATION_IDEMPOTENCY_MIGRATION_CHECKSUM
    ):
        raise RuntimeError("Recipe creation idempotency migration checksum changed")
    connection.execute(
        "INSERT OR IGNORE INTO registry_schema_migration VALUES (?, ?, ?)",
        [
            RECIPE_CREATION_IDEMPOTENCY_MIGRATION_ID,
            RECIPE_CREATION_IDEMPOTENCY_MIGRATION_CHECKSUM,
            datetime.now(timezone.utc).isoformat(),
        ],
    )


def _migrate_clean_recipe_root(connection: duckdb.DuckDBPyConnection) -> None:
    """Remove superseded bootstrap fields and make cutover history append-only."""

    connection.execute(
        "DELETE FROM recipe_intent WHERE kind = 'RECIPE_DELETION'"
    )
    connection.execute("DROP TABLE IF EXISTS recipe_deletion_target")
    connection.execute("DROP TABLE IF EXISTS unlinked_recipe_workspace")

    recipe_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info('recipe')").fetchall()
    }
    if "state" in recipe_columns:
        connection.execute("UPDATE recipe SET state = 'ACTIVE' WHERE state = 'DELETING'")
        connection.execute(
            """
            CREATE TABLE recipe_clean_root (
                recipe_id VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                business_purpose VARCHAR NOT NULL,
                data_classification VARCHAR NOT NULL,
                retention_days INTEGER NOT NULL,
                current_recipe_revision INTEGER,
                current_data_version_id VARCHAR,
                cutover_candidate_id VARCHAR,
                optimistic_revision INTEGER NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recipe_clean_root
            SELECT recipe_id, display_name, business_purpose,
                   data_classification, retention_days, current_recipe_revision,
                   current_data_version_id, cutover_candidate_id,
                   optimistic_revision, created_at, updated_at
              FROM recipe
            """
        )
        connection.execute("DROP TABLE recipe")
        connection.execute("ALTER TABLE recipe_clean_root RENAME TO recipe")

    data_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info('data_version')").fetchall()
    }
    if "intake_status" in data_columns:
        connection.execute(
            """
            CREATE TABLE data_version_clean_root (
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
                created_at VARCHAR NOT NULL,
                sealed_at VARCHAR,
                UNIQUE (recipe_id, version_number)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO data_version_clean_root
            SELECT data_version_id, recipe_id, version_number,
                   workspace_project_id, parent_data_version_id, purpose,
                   CASE WHEN state IN ('ACTIVE', 'SEALED') THEN state ELSE 'SEALED' END,
                   pinned_recipe_revision, label, export_as_of_date,
                   parameter_values_hash, created_at, sealed_at
              FROM data_version
            """
        )
        connection.execute("DROP TABLE data_version")
        connection.execute(
            "ALTER TABLE data_version_clean_root RENAME TO data_version"
        )

    intent_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info('recipe_intent')").fetchall()
    }
    if "retry_count" in intent_columns:
        connection.execute(
            """
            CREATE TABLE recipe_intent_clean_root (
                operation_id VARCHAR PRIMARY KEY,
                recipe_id VARCHAR NOT NULL,
                kind VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                expected_recipe_revision INTEGER NOT NULL,
                detail_json VARCHAR NOT NULL,
                last_error VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recipe_intent_clean_root
            SELECT operation_id, recipe_id, kind, state,
                   expected_recipe_revision, detail_json, last_error,
                   created_at, updated_at
              FROM recipe_intent
            """
        )
        connection.execute("DROP TABLE recipe_intent")
        connection.execute(
            "ALTER TABLE recipe_intent_clean_root RENAME TO recipe_intent"
        )

    legacy_cutover_constraint = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM duckdb_constraints()
             WHERE table_name = 'cutover_candidate'
               AND constraint_type = 'UNIQUE'
               AND constraint_column_names = ['recipe_id']
        )
        """
    ).fetchone()
    if legacy_cutover_constraint and bool(legacy_cutover_constraint[0]):
        connection.execute(
            """
            CREATE TABLE cutover_candidate_history (
                cutover_candidate_id VARCHAR PRIMARY KEY,
                recipe_id VARCHAR NOT NULL,
                recipe_revision INTEGER NOT NULL,
                qualification_id VARCHAR NOT NULL,
                expected_recipe_revision INTEGER NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL,
                selected_at VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO cutover_candidate_history SELECT * FROM cutover_candidate"
        )
        connection.execute("DROP TABLE cutover_candidate")
        connection.execute(
            "ALTER TABLE cutover_candidate_history RENAME TO cutover_candidate"
        )


def _migrate_recipe_creation_idempotency(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Add replay identity without changing earlier checksum-pinned migrations."""

    connection.execute(
        "ALTER TABLE recipe "
        "ADD COLUMN IF NOT EXISTS creation_request_id VARCHAR"
    )
    connection.execute(
        "ALTER TABLE recipe "
        "ADD COLUMN IF NOT EXISTS creation_request_hash VARCHAR"
    )
    connection.execute(
        "ALTER TABLE project_registry_sync_pending "
        "ADD COLUMN IF NOT EXISTS creation_request_id VARCHAR"
    )
    connection.execute(
        "ALTER TABLE project_registry_sync_pending "
        "ADD COLUMN IF NOT EXISTS creation_request_hash VARCHAR"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS recipe_creation_request_id_unique "
        "ON recipe (creation_request_id)"
    )
