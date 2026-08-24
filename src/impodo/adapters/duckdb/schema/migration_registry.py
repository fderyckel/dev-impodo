"""Create and validate the exact clean-root Migration Project registry."""

from __future__ import annotations

from pathlib import Path

import duckdb

from ....migration_foundation import MigrationStorageCompatibilityError


MIGRATION_REGISTRY_GENERATION = "impodo-migration-registry-2026-08-project-root"
MIGRATION_REGISTRY_VERSION = 1


EXPECTED_REGISTRY_COLUMNS = {
    "schema_version": (
        "singleton_id",
        "generation",
        "version",
    ),
    "migration_project_identity": ("project_id",),
    "migration_project": (
        "project_id",
        "display_name",
        "migration_purpose",
        "source_system_identity",
        "data_classification",
        "retention_days",
        "status",
        "optimistic_revision",
        "created_at",
        "updated_at",
        "closed_at",
        "archived_at",
    ),
    "data_version_identity": ("data_version_id",),
    "data_version": (
        "data_version_id",
        "project_id",
        "version_number",
        "parent_data_version_id",
        "purpose",
        "state",
        "label",
        "export_as_of",
        "source_package_hash",
        "optimistic_revision",
        "created_at",
        "updated_at",
        "frozen_at",
    ),
    "migration_run_identity": ("migration_run_id",),
    "migration_run": (
        "migration_run_id",
        "project_id",
        "data_version_id",
        "run_number",
        "purpose",
        "label",
        "state",
        "target_binding_id",
        "cutover_selection_id",
        "optimistic_revision",
        "created_at",
        "updated_at",
        "closed_at",
    ),
    "migration_run_target_setup": (
        "migration_run_id",
        "project_id",
        "revision",
        "connection_mode",
        "base_url",
        "database",
        "intended_applications_json",
        "updated_at",
    ),
    "migration_workspace_identity": ("workspace_id",),
    "migration_workspace": (
        "workspace_id",
        "project_id",
        "data_version_id",
        "migration_run_id",
        "recipe_application_id",
        "display_name",
        "state",
        "setup_state",
        "optimistic_revision",
        "created_at",
        "updated_at",
        "setup_completed_at",
        "closed_at",
    ),
    "target_binding": (
        "target_binding_id",
        "project_id",
        "migration_run_id",
        "environment",
        "connection_target_hash",
        "credential_role",
        "credential_generation",
        "principal_hash",
        "permission_hash",
        "context_hash",
        "schema_dependency_hash",
        "reference_snapshot_hashes_json",
        "content_hash",
        "created_at",
    ),
    "migration_run_requirement_plan": (
        "migration_run_id",
        "project_id",
        "data_version_id",
        "target_binding_id",
        "contract_version",
        "selected_revisions_json",
        "dependencies_json",
        "model_requirements_json",
        "reference_requirements_json",
        "application_order_json",
        "content_hash",
        "created_at",
    ),
    "migration_run_target_schema": (
        "migration_run_id",
        "target_binding_id",
        "requirement_plan_hash",
        "schema_hash",
        "schema_json",
        "captured_at",
    ),
    "migration_run_reference_bundle": (
        "migration_run_id",
        "target_binding_id",
        "bundle_hash",
        "bundle_json",
    ),
    "migration_run_cutover_plan": (
        "migration_run_id",
        "cutover_plan_id",
        "cutover_plan_revision",
        "plan_content_hash",
        "bound_at",
    ),
    "recipe_identity": ("recipe_id",),
    "recipe": (
        "recipe_id",
        "project_id",
        "display_name",
        "business_purpose",
        "current_recipe_revision",
        "optimistic_revision",
        "created_at",
        "updated_at",
        "archived_at",
    ),
    "recipe_revision": (
        "recipe_id",
        "version",
        "parent_version",
        "semantic_hash",
        "payload_hash",
        "storage_key",
        "artifact_hash",
        "contract_versions_json",
        "provenance_json",
        "published_at",
    ),
    "recipe_application_identity": ("application_id",),
    "recipe_application": (
        "application_id",
        "project_id",
        "migration_run_id",
        "data_version_id",
        "workspace_id",
        "recipe_id",
        "recipe_revision",
        "recipe_semantic_hash",
        "target_binding_id",
        "physical_binding_hash",
        "parameter_values_hash",
        "status",
        "issue_hash",
        "mapping_id",
        "mapping_content_hash",
        "evidence_hash",
        "created_at",
        "updated_at",
    ),
    "recipe_application_issue": (
        "application_id",
        "ordinal",
        "code",
        "level",
        "message",
        "recovery_action",
        "recipe_ids_json",
        "content_hash",
    ),
    "recipe_application_requirement": (
        "application_id",
        "model",
        "fields_json",
        "content_hash",
    ),
    "recipe_application_reference_requirement": (
        "application_id",
        "name",
        "reference_hash",
        "content_hash",
    ),
    "recipe_qualification": (
        "qualification_id",
        "project_id",
        "recipe_id",
        "recipe_revision",
        "application_id",
        "test_target_binding_hash",
        "status",
        "expected_outcomes_json",
        "evidence_storage_key",
        "artifact_hash",
        "evidence_hash",
        "qualified_by_issuer",
        "qualified_by_subject",
        "qualified_by_display_name",
        "qualified_at",
    ),
    "cutover_plan_identity": ("cutover_plan_id",),
    "cutover_plan": (
        "cutover_plan_id",
        "project_id",
        "display_name",
        "current_revision",
        "optimistic_revision",
        "created_at",
        "updated_at",
        "archived_at",
    ),
    "cutover_plan_revision": (
        "cutover_plan_id",
        "version",
        "parent_version",
        "shared_controls_json",
        "requirement_plan_hash",
        "meaning_hash",
        "content_hash",
        "created_by_issuer",
        "created_by_subject",
        "created_by_display_name",
        "created_at",
    ),
    "cutover_plan_recipe": (
        "cutover_plan_id",
        "plan_revision",
        "recipe_id",
        "recipe_revision",
        "semantic_hash",
    ),
    "cutover_dependency": (
        "cutover_plan_id",
        "plan_revision",
        "before_recipe_id",
        "after_recipe_id",
        "kind",
        "reason",
    ),
    "cutover_write_ownership": (
        "cutover_plan_id",
        "plan_revision",
        "recipe_id",
        "model",
        "field",
    ),
    "cutover_plan_qualification": (
        "qualification_id",
        "project_id",
        "cutover_plan_id",
        "cutover_plan_revision",
        "plan_content_hash",
        "test_run_id",
        "application_ids_json",
        "application_qualification_ids_json",
        "target_binding_hash",
        "requirement_plan_hash",
        "integrated_evidence_hash",
        "evidence_storage_key",
        "artifact_hash",
        "status",
        "qualified_by_issuer",
        "qualified_by_subject",
        "qualified_by_display_name",
        "qualified_at",
    ),
    "project_cutover_selection": (
        "cutover_selection_id",
        "project_id",
        "cutover_plan_id",
        "cutover_plan_revision",
        "qualification_id",
        "content_hash",
        "selected_by_issuer",
        "selected_by_subject",
        "selected_by_display_name",
        "selected_at",
    ),
    "production_run_binding": (
        "production_run_binding_id",
        "project_id",
        "migration_run_id",
        "data_version_id",
        "setup_workspace_id",
        "cutover_selection_id",
        "qualification_id",
        "cutover_plan_id",
        "cutover_plan_revision",
        "plan_content_hash",
        "test_target_binding_hash",
        "state",
        "target_binding_id",
        "read_credential_generation",
        "write_credential_generation",
        "write_principal_hash",
        "write_permission_hash",
        "write_context_hash",
        "parameter_values_hash",
        "control_values_hash",
        "activation_evidence_hash",
        "content_hash",
        "created_at",
        "activated_at",
        "contract_version",
    ),
    "project_operation_intent": (
        "operation_id",
        "project_id",
        "owner_kind",
        "owner_id",
        "kind",
        "request_hash",
        "expected_revision",
        "state",
        "stage",
        "detail_json",
        "result_json",
        "last_error",
        "actor_issuer",
        "actor_subject",
        "actor_display_name",
        "created_at",
        "updated_at",
    ),
    "migration_event": (
        "event_id",
        "project_id",
        "aggregate_kind",
        "aggregate_id",
        "aggregate_revision",
        "event_type",
        "detail_json",
        "actor_issuer",
        "actor_subject",
        "actor_display_name",
        "occurred_at",
    ),
}


def ensure_migration_registry_schema(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
) -> None:
    """Create the current empty registry or reject every other schema."""

    tables = _tables(connection)
    if not tables:
        _initialize_migration_registry(connection)
        return
    if set(tables) != set(EXPECTED_REGISTRY_COLUMNS):
        raise _compatibility_error(database_path)
    try:
        row = connection.execute(
            "SELECT generation, version FROM schema_version WHERE singleton_id = 1"
        ).fetchone()
    except duckdb.Error as error:
        raise _compatibility_error(database_path) from error
    if row != (MIGRATION_REGISTRY_GENERATION, MIGRATION_REGISTRY_VERSION):
        raise _compatibility_error(database_path)
    for table, expected in EXPECTED_REGISTRY_COLUMNS.items():
        actual = tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        )
        if actual != expected:
            raise _compatibility_error(database_path)


def _initialize_migration_registry(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    connection.begin()
    try:
        connection.execute(
            f"""
            CREATE TABLE schema_version (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                generation VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version VALUES (
                1, '{MIGRATION_REGISTRY_GENERATION}', {MIGRATION_REGISTRY_VERSION}
            );

            CREATE TABLE migration_project_identity (
                project_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE migration_project (
                project_id VARCHAR PRIMARY KEY REFERENCES
                    migration_project_identity(project_id),
                display_name VARCHAR NOT NULL,
                migration_purpose VARCHAR NOT NULL,
                source_system_identity VARCHAR NOT NULL,
                data_classification VARCHAR NOT NULL,
                retention_days INTEGER NOT NULL CHECK (
                    retention_days BETWEEN 1 AND 3650
                ),
                status VARCHAR NOT NULL CHECK (
                    status IN ('DRAFT', 'ACTIVE', 'CLOSED', 'ARCHIVED')
                ),
                optimistic_revision INTEGER NOT NULL CHECK (
                    optimistic_revision >= 1
                ),
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                closed_at VARCHAR,
                archived_at VARCHAR
            );

            CREATE TABLE data_version_identity (
                data_version_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE data_version (
                data_version_id VARCHAR PRIMARY KEY REFERENCES
                    data_version_identity(data_version_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                version_number INTEGER NOT NULL CHECK (version_number >= 1),
                parent_data_version_id VARCHAR REFERENCES
                    data_version_identity(data_version_id),
                purpose VARCHAR NOT NULL CHECK (
                    purpose IN ('AUTHORING', 'TEST', 'PRODUCTION')
                ),
                state VARCHAR NOT NULL CHECK (state IN ('DRAFT', 'FROZEN')),
                label VARCHAR NOT NULL,
                export_as_of VARCHAR NOT NULL,
                source_package_hash VARCHAR,
                optimistic_revision INTEGER NOT NULL CHECK (
                    optimistic_revision >= 1
                ),
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                frozen_at VARCHAR,
                UNIQUE (project_id, version_number)
            );

            CREATE TABLE migration_run_identity (
                migration_run_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE migration_run (
                migration_run_id VARCHAR PRIMARY KEY REFERENCES
                    migration_run_identity(migration_run_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                data_version_id VARCHAR NOT NULL REFERENCES
                    data_version_identity(data_version_id),
                run_number INTEGER NOT NULL CHECK (run_number >= 1),
                purpose VARCHAR NOT NULL CHECK (
                    purpose IN ('AUTHORING', 'TEST', 'PRODUCTION')
                ),
                label VARCHAR NOT NULL,
                state VARCHAR NOT NULL CHECK (
                    state IN (
                        'DRAFT', 'READY', 'RUNNING', 'INCOMPLETE',
                        'COMPLETED', 'CLOSED'
                    )
                ),
                target_binding_id VARCHAR,
                cutover_selection_id VARCHAR,
                optimistic_revision INTEGER NOT NULL CHECK (
                    optimistic_revision >= 1
                ),
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                closed_at VARCHAR,
                UNIQUE (project_id, run_number)
            );

            CREATE TABLE migration_workspace_identity (
                workspace_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE migration_run_target_setup (
                migration_run_id VARCHAR PRIMARY KEY REFERENCES
                    migration_run_identity(migration_run_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                connection_mode VARCHAR NOT NULL CHECK (
                    connection_mode IN ('LOCAL', 'REMOTE')
                ),
                base_url VARCHAR NOT NULL,
                database VARCHAR NOT NULL,
                intended_applications_json VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            );

            CREATE TABLE migration_workspace (
                workspace_id VARCHAR PRIMARY KEY REFERENCES
                    migration_workspace_identity(workspace_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                data_version_id VARCHAR NOT NULL REFERENCES
                    data_version_identity(data_version_id),
                migration_run_id VARCHAR NOT NULL REFERENCES
                    migration_run_identity(migration_run_id),
                recipe_application_id VARCHAR UNIQUE,
                display_name VARCHAR NOT NULL,
                state VARCHAR NOT NULL CHECK (state IN ('OPEN', 'CLOSED')),
                setup_state VARCHAR NOT NULL CHECK (
                    setup_state IN ('DRAFT', 'READY')
                ),
                optimistic_revision INTEGER NOT NULL CHECK (
                    optimistic_revision >= 1
                ),
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                setup_completed_at VARCHAR,
                closed_at VARCHAR
            );

            CREATE TABLE target_binding (
                target_binding_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                migration_run_id VARCHAR NOT NULL UNIQUE REFERENCES
                    migration_run_identity(migration_run_id),
                environment VARCHAR NOT NULL,
                connection_target_hash VARCHAR NOT NULL,
                credential_role VARCHAR NOT NULL,
                credential_generation VARCHAR NOT NULL,
                principal_hash VARCHAR NOT NULL,
                permission_hash VARCHAR NOT NULL,
                context_hash VARCHAR NOT NULL,
                schema_dependency_hash VARCHAR NOT NULL,
                reference_snapshot_hashes_json VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL
            );

            CREATE TABLE migration_run_requirement_plan (
                migration_run_id VARCHAR PRIMARY KEY REFERENCES
                    migration_run_identity(migration_run_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                data_version_id VARCHAR NOT NULL REFERENCES
                    data_version_identity(data_version_id),
                target_binding_id VARCHAR NOT NULL REFERENCES
                    target_binding(target_binding_id),
                contract_version INTEGER NOT NULL CHECK (contract_version = 1),
                selected_revisions_json VARCHAR NOT NULL,
                dependencies_json VARCHAR NOT NULL,
                model_requirements_json VARCHAR NOT NULL,
                reference_requirements_json VARCHAR NOT NULL,
                application_order_json VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL
            );

            CREATE TABLE migration_run_target_schema (
                migration_run_id VARCHAR PRIMARY KEY REFERENCES
                    migration_run_identity(migration_run_id),
                target_binding_id VARCHAR NOT NULL REFERENCES
                    target_binding(target_binding_id),
                requirement_plan_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                schema_json VARCHAR NOT NULL,
                captured_at VARCHAR NOT NULL
            );

            CREATE TABLE migration_run_reference_bundle (
                migration_run_id VARCHAR PRIMARY KEY REFERENCES
                    migration_run_identity(migration_run_id),
                target_binding_id VARCHAR NOT NULL REFERENCES
                    target_binding(target_binding_id),
                bundle_hash VARCHAR NOT NULL,
                bundle_json VARCHAR NOT NULL
            );

            CREATE TABLE recipe_identity (
                recipe_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE recipe (
                recipe_id VARCHAR PRIMARY KEY REFERENCES
                    recipe_identity(recipe_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                display_name VARCHAR NOT NULL,
                business_purpose VARCHAR NOT NULL,
                current_recipe_revision INTEGER,
                optimistic_revision INTEGER NOT NULL CHECK (
                    optimistic_revision >= 1
                ),
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                archived_at VARCHAR
            );

            CREATE TABLE recipe_revision (
                recipe_id VARCHAR NOT NULL REFERENCES
                    recipe_identity(recipe_id),
                version INTEGER NOT NULL CHECK (version >= 1),
                parent_version INTEGER,
                semantic_hash VARCHAR NOT NULL,
                payload_hash VARCHAR NOT NULL,
                storage_key VARCHAR NOT NULL,
                artifact_hash VARCHAR NOT NULL,
                contract_versions_json VARCHAR NOT NULL,
                provenance_json VARCHAR NOT NULL,
                published_at VARCHAR NOT NULL,
                PRIMARY KEY (recipe_id, version),
                UNIQUE (recipe_id, semantic_hash)
            );

            CREATE TABLE recipe_application_identity (
                application_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE recipe_application (
                application_id VARCHAR PRIMARY KEY REFERENCES
                    recipe_application_identity(application_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                migration_run_id VARCHAR NOT NULL REFERENCES
                    migration_run_identity(migration_run_id),
                data_version_id VARCHAR NOT NULL REFERENCES
                    data_version_identity(data_version_id),
                workspace_id VARCHAR NOT NULL UNIQUE REFERENCES
                    migration_workspace_identity(workspace_id),
                recipe_id VARCHAR NOT NULL,
                recipe_revision INTEGER NOT NULL,
                recipe_semantic_hash VARCHAR NOT NULL,
                target_binding_id VARCHAR NOT NULL REFERENCES
                    target_binding(target_binding_id),
                physical_binding_hash VARCHAR NOT NULL,
                parameter_values_hash VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                issue_hash VARCHAR NOT NULL,
                mapping_id VARCHAR,
                mapping_content_hash VARCHAR,
                evidence_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                UNIQUE (migration_run_id, recipe_id),
                FOREIGN KEY (recipe_id, recipe_revision)
                    REFERENCES recipe_revision(recipe_id, version)
            );

            CREATE TABLE recipe_application_issue (
                application_id VARCHAR NOT NULL REFERENCES
                    recipe_application_identity(application_id),
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                code VARCHAR NOT NULL,
                level VARCHAR NOT NULL CHECK (
                    level IN ('BLOCKER', 'REVIEW', 'INFORMATION')
                ),
                message VARCHAR NOT NULL,
                recovery_action VARCHAR NOT NULL,
                recipe_ids_json VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                PRIMARY KEY (application_id, ordinal)
            );

            CREATE TABLE recipe_application_requirement (
                application_id VARCHAR NOT NULL REFERENCES
                    recipe_application_identity(application_id),
                model VARCHAR NOT NULL,
                fields_json VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                PRIMARY KEY (application_id, model)
            );

            CREATE TABLE recipe_application_reference_requirement (
                application_id VARCHAR NOT NULL REFERENCES
                    recipe_application_identity(application_id),
                name VARCHAR NOT NULL,
                reference_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                PRIMARY KEY (application_id, name)
            );

            CREATE TABLE recipe_qualification (
                qualification_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                recipe_id VARCHAR NOT NULL,
                recipe_revision INTEGER NOT NULL,
                application_id VARCHAR NOT NULL UNIQUE REFERENCES
                    recipe_application_identity(application_id),
                test_target_binding_hash VARCHAR NOT NULL,
                status VARCHAR NOT NULL CHECK (status = 'TEST_QUALIFIED'),
                expected_outcomes_json VARCHAR NOT NULL,
                evidence_storage_key VARCHAR NOT NULL,
                artifact_hash VARCHAR NOT NULL,
                evidence_hash VARCHAR NOT NULL,
                qualified_by_issuer VARCHAR NOT NULL,
                qualified_by_subject VARCHAR NOT NULL,
                qualified_by_display_name VARCHAR NOT NULL,
                qualified_at VARCHAR NOT NULL,
                FOREIGN KEY (recipe_id, recipe_revision)
                    REFERENCES recipe_revision(recipe_id, version)
            );

            CREATE TABLE cutover_plan_identity (
                cutover_plan_id VARCHAR PRIMARY KEY
            );

            CREATE TABLE cutover_plan (
                cutover_plan_id VARCHAR PRIMARY KEY REFERENCES
                    cutover_plan_identity(cutover_plan_id),
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                display_name VARCHAR NOT NULL,
                current_revision INTEGER,
                optimistic_revision INTEGER NOT NULL CHECK (
                    optimistic_revision >= 1
                ),
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                archived_at VARCHAR
            );

            CREATE TABLE cutover_plan_revision (
                cutover_plan_id VARCHAR NOT NULL REFERENCES
                    cutover_plan_identity(cutover_plan_id),
                version INTEGER NOT NULL CHECK (version >= 1),
                parent_version INTEGER,
                shared_controls_json VARCHAR NOT NULL,
                requirement_plan_hash VARCHAR NOT NULL,
                meaning_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                created_by_issuer VARCHAR NOT NULL,
                created_by_subject VARCHAR NOT NULL,
                created_by_display_name VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                PRIMARY KEY (cutover_plan_id, version),
                UNIQUE (cutover_plan_id, meaning_hash)
            );

            CREATE TABLE cutover_plan_recipe (
                cutover_plan_id VARCHAR NOT NULL,
                plan_revision INTEGER NOT NULL,
                recipe_id VARCHAR NOT NULL,
                recipe_revision INTEGER NOT NULL,
                semantic_hash VARCHAR NOT NULL,
                PRIMARY KEY (cutover_plan_id, plan_revision, recipe_id),
                FOREIGN KEY (cutover_plan_id, plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version),
                FOREIGN KEY (recipe_id, recipe_revision)
                    REFERENCES recipe_revision(recipe_id, version)
            );

            CREATE TABLE cutover_dependency (
                cutover_plan_id VARCHAR NOT NULL,
                plan_revision INTEGER NOT NULL,
                before_recipe_id VARCHAR NOT NULL REFERENCES
                    recipe_identity(recipe_id),
                after_recipe_id VARCHAR NOT NULL REFERENCES
                    recipe_identity(recipe_id),
                kind VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                PRIMARY KEY (
                    cutover_plan_id, plan_revision,
                    before_recipe_id, after_recipe_id
                ),
                FOREIGN KEY (cutover_plan_id, plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version)
            );

            CREATE TABLE cutover_write_ownership (
                cutover_plan_id VARCHAR NOT NULL,
                plan_revision INTEGER NOT NULL,
                recipe_id VARCHAR NOT NULL REFERENCES
                    recipe_identity(recipe_id),
                model VARCHAR NOT NULL,
                field VARCHAR NOT NULL,
                PRIMARY KEY (cutover_plan_id, plan_revision, model, field),
                FOREIGN KEY (cutover_plan_id, plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version)
            );

            CREATE TABLE migration_run_cutover_plan (
                migration_run_id VARCHAR PRIMARY KEY REFERENCES
                    migration_run_identity(migration_run_id),
                cutover_plan_id VARCHAR NOT NULL,
                cutover_plan_revision INTEGER NOT NULL,
                plan_content_hash VARCHAR NOT NULL,
                bound_at VARCHAR NOT NULL,
                FOREIGN KEY (cutover_plan_id, cutover_plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version)
            );

            CREATE TABLE cutover_plan_qualification (
                qualification_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                cutover_plan_id VARCHAR NOT NULL,
                cutover_plan_revision INTEGER NOT NULL,
                plan_content_hash VARCHAR NOT NULL,
                test_run_id VARCHAR NOT NULL REFERENCES
                    migration_run_identity(migration_run_id),
                application_ids_json VARCHAR NOT NULL,
                application_qualification_ids_json VARCHAR NOT NULL,
                target_binding_hash VARCHAR NOT NULL,
                requirement_plan_hash VARCHAR NOT NULL,
                integrated_evidence_hash VARCHAR NOT NULL,
                evidence_storage_key VARCHAR NOT NULL,
                artifact_hash VARCHAR NOT NULL,
                status VARCHAR NOT NULL CHECK (status = 'TEST_QUALIFIED'),
                qualified_by_issuer VARCHAR NOT NULL,
                qualified_by_subject VARCHAR NOT NULL,
                qualified_by_display_name VARCHAR NOT NULL,
                qualified_at VARCHAR NOT NULL,
                FOREIGN KEY (cutover_plan_id, cutover_plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version)
            );

            CREATE TABLE project_cutover_selection (
                cutover_selection_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                cutover_plan_id VARCHAR NOT NULL,
                cutover_plan_revision INTEGER NOT NULL,
                qualification_id VARCHAR NOT NULL
                    REFERENCES cutover_plan_qualification(qualification_id),
                content_hash VARCHAR NOT NULL,
                selected_by_issuer VARCHAR NOT NULL,
                selected_by_subject VARCHAR NOT NULL,
                selected_by_display_name VARCHAR NOT NULL,
                selected_at VARCHAR NOT NULL,
                FOREIGN KEY (cutover_plan_id, cutover_plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version)
            );

            CREATE TABLE production_run_binding (
                production_run_binding_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                migration_run_id VARCHAR NOT NULL UNIQUE REFERENCES
                    migration_run_identity(migration_run_id),
                data_version_id VARCHAR NOT NULL REFERENCES
                    data_version_identity(data_version_id),
                setup_workspace_id VARCHAR NOT NULL UNIQUE REFERENCES
                    migration_workspace_identity(workspace_id),
                cutover_selection_id VARCHAR NOT NULL REFERENCES
                    project_cutover_selection(cutover_selection_id),
                qualification_id VARCHAR NOT NULL REFERENCES
                    cutover_plan_qualification(qualification_id),
                cutover_plan_id VARCHAR NOT NULL,
                cutover_plan_revision INTEGER NOT NULL,
                plan_content_hash VARCHAR NOT NULL,
                test_target_binding_hash VARCHAR NOT NULL,
                state VARCHAR NOT NULL CHECK (state IN ('SETUP', 'ACTIVE')),
                target_binding_id VARCHAR REFERENCES
                    target_binding(target_binding_id),
                read_credential_generation VARCHAR,
                write_credential_generation VARCHAR,
                write_principal_hash VARCHAR,
                write_permission_hash VARCHAR,
                write_context_hash VARCHAR,
                parameter_values_hash VARCHAR,
                control_values_hash VARCHAR,
                activation_evidence_hash VARCHAR,
                content_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                activated_at VARCHAR,
                contract_version INTEGER NOT NULL CHECK (contract_version = 1),
                FOREIGN KEY (cutover_plan_id, cutover_plan_revision)
                    REFERENCES cutover_plan_revision(cutover_plan_id, version)
            );

            CREATE TABLE project_operation_intent (
                operation_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL,
                owner_kind VARCHAR NOT NULL,
                owner_id VARCHAR NOT NULL,
                kind VARCHAR NOT NULL,
                request_hash VARCHAR NOT NULL,
                expected_revision INTEGER,
                state VARCHAR NOT NULL CHECK (
                    state IN ('PENDING', 'COMMITTED', 'FAILED')
                ),
                stage VARCHAR NOT NULL,
                detail_json VARCHAR NOT NULL,
                result_json VARCHAR NOT NULL,
                last_error VARCHAR NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            );

            CREATE TABLE migration_event (
                event_id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES
                    migration_project_identity(project_id),
                aggregate_kind VARCHAR NOT NULL,
                aggregate_id VARCHAR NOT NULL,
                aggregate_revision INTEGER NOT NULL CHECK (
                    aggregate_revision >= 1
                ),
                event_type VARCHAR NOT NULL,
                detail_json VARCHAR NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL,
                occurred_at VARCHAR NOT NULL
            );
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _tables(connection: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    return tuple(
        sorted(str(row[0]) for row in connection.execute("SHOW TABLES").fetchall())
    )


def _compatibility_error(database_path: Path) -> MigrationStorageCompatibilityError:
    root = database_path.resolve().parent
    command = (
        ".\\.venv\\Scripts\\python.exe scripts\\reset-development-storage.py "
        f'--root "{root}"'
    )
    return MigrationStorageCompatibilityError(str(database_path.resolve()), command)
