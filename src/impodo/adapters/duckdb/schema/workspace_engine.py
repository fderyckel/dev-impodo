"""Creation and exact validation of current workspace-engine DuckDB files."""

from __future__ import annotations

import duckdb

from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError
from ..constants import (
    SCHEMA_BASELINE_VERSION,
    SCHEMA_GENERATION,
    SCHEMA_VERSION,
)
from .advanced_coverage import create_advanced_coverage_schema
from .derived_value_artifact import create_derived_value_artifact_schema
from .execution import create_execution_schema
from .forward_upgrades import (
    SCHEMA_MIGRATION_COLUMNS,
    ForwardSchemaUpgrade,
    create_schema_migration_ledger,
    ensure_current_schema,
)
from .preflight import create_preflight_schema
from .preparation_session import create_preparation_session_schema
from .prepared_snapshot import create_prepared_snapshot_schema
from .reconciliation import create_reconciliation_schema
from .recipe_compilation import (
    create_recipe_compilation_schema,
)
from .supporting_lookup import create_supporting_lookup_schema
from .source_snapshot import create_source_snapshot_schema


_WORKSPACE_PROJECTION_COLUMNS = (
    "singleton_id",
    "name",
    "source_system",
    "source_mode",
    "data_classification",
    "retention_days",
    "odoo_connection_mode",
    "odoo_base_url",
    "odoo_database",
    "intended_applications",
    "intended_models",
    "status",
    "revision",
    "created_at",
    "updated_at",
    "registered_at",
    "mapping_version",
    "current_run_id",
    "approval_status",
)
_AUDIT_EVENT_COLUMNS = (
    "event_id",
    "event_type",
    "workspace_revision",
    "occurred_at",
    "detail",
    "actor_issuer",
    "actor_subject",
    "actor_display_name",
)
_MAPPING_MUTATION_RECEIPT_COLUMNS = (
    "operation_id",
    "action",
    "request_hash",
    "state",
    "submitted_working_draft_version",
    "submitted_mapping_revision_version",
    "working_draft_version",
    "mapping_revision_version",
    "content_identity",
    "failure_code",
    "failure_detail",
    "started_at",
    "completed_at",
    "actor_issuer",
    "actor_subject",
)
_ODOO_CAPTURE_SELECTION_CURRENT_COLUMNS = (
    "model",
    "selection_id",
    "version",
)
_ODOO_CAPTURE_MANIFEST_CURRENT_COLUMNS = (
    "dataset_id",
    "manifest_id",
)
_SCHEMA_MIGRATION_COLUMNS = SCHEMA_MIGRATION_COLUMNS
_WORKSPACE_ENGINE_TABLES = frozenset(
    {
        "audit_event", "canonical_prepared_projection", "canonical_staging_current",
        "canonical_staging_row", "canonical_staging_row_issue", "canonical_staging_run",
        "coverage_scope_current", "coverage_scope_revision",
        "derived_entity_plan_current", "derived_entity_plan_revision",
        "derived_value_artifact_current", "derived_value_artifact_manifest",
        "effective_dataset_current", "effective_dataset_reconciliation", "effective_row",
        "execution_current", "execution_row", "execution_run", "mapping_current",
        "mapping_mutation_receipt", "mapping_revision", "mapping_submission", "mapping_validation",
        "mapping_working_draft", "normalization_current", "normalization_effect",
        "normalization_group", "normalization_run", "normalization_transition",
        "odoo_capture_manifest_current", "odoo_capture_manifest_revision",
        "odoo_capture_selection_current", "odoo_capture_selection_revision",
        "odoo_model_catalog", "odoo_schema_catalog", "preflight_current",
        "preflight_dataset", "preflight_decision", "preflight_target_snapshot",
        "preflight_transition", "preparation_direct_identity",
        "preparation_identity_group", "preparation_impact_row", "preparation_lineage",
        "preparation_normalization_finding", "preparation_normalization_group_seed",
        "preparation_physical_row", "preparation_relationship_edge",
        "preparation_session", "preparation_session_derived_artifact",
        "preparation_session_snapshot", "prepared_snapshot_current",
        "prepared_snapshot_manifest", "quality_current", "quality_evidence_projection",
        "quality_issue", "quality_quarantine_entry", "quality_row_result",
        "quality_ruleset_current", "quality_ruleset_revision", "quality_run",
        "readiness_run", "recipe_parameter_definitions", "recipe_quality_seed",
        "reconciliation_current", "reconciliation_run", "reference_bundle_current",
        "reference_bundle_revision", "resolution_accounting", "resolution_candidate",
        "resolution_current", "resolution_decision", "resolution_finding",
        "resolution_policy_current", "resolution_policy_revision", "resolution_run",
        "retired_evidence", "schema_governance_current", "schema_governance_revision",
        "schema_migration", "schema_version", "source_accounting_entry",
        "source_accounting_link",
        "source_catalog", "source_configuration", "source_file", "source_selection",
        "source_snapshot_current", "source_snapshot_manifest",
        "supporting_lookup_current", "supporting_lookup_revision",
        "transformation_impact_row", "transformation_impact_run",
        "transformation_rule_acknowledgement", "transformation_rule_impact",
        "workspace_projection_cache",
    }
)


_UNSUPPORTED_WORKSPACE_MESSAGE = (
    "This workspace uses a saved-data generation or version that this Impodo "
    "build cannot safely open. Impodo left its evidence unchanged. Update "
    "Impodo if a newer release created it, or return to Projects for the "
    "available recovery action."
)


class WorkspaceEngineSchemaMixin:
    """Create or upgrade the current workspace-engine schema."""

    def _initialize_workspace_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE schema_version (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                generation VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version VALUES (
                1, '{SCHEMA_GENERATION}', {SCHEMA_VERSION}
            );

            CREATE TABLE workspace_projection_cache (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                name VARCHAR NOT NULL,
                source_system VARCHAR NOT NULL,
                source_mode VARCHAR NOT NULL,
                data_classification VARCHAR NOT NULL,
                retention_days INTEGER NOT NULL,
                odoo_connection_mode VARCHAR,
                odoo_base_url VARCHAR NOT NULL,
                odoo_database VARCHAR NOT NULL,
                intended_applications VARCHAR NOT NULL,
                intended_models VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                registered_at VARCHAR,
                mapping_version VARCHAR,
                current_run_id VARCHAR,
                approval_status VARCHAR NOT NULL
            );

            CREATE TABLE source_file (
                file_id VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                stored_name VARCHAR NOT NULL,
                size_bytes BIGINT NOT NULL,
                sha256 VARCHAR NOT NULL,
                received_at VARCHAR NOT NULL
            );

            CREATE TABLE source_catalog (
                file_id VARCHAR PRIMARY KEY,
                source_sha256 VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                inspected_at VARCHAR NOT NULL,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE source_configuration (
                file_id VARCHAR PRIMARY KEY,
                source_sha256 VARCHAR NOT NULL,
                catalog_hash VARCHAR NOT NULL,
                configuration_json VARCHAR NOT NULL
            );

            CREATE TABLE source_selection (
                singleton_id INTEGER PRIMARY KEY,
                selection_json VARCHAR NOT NULL
            );

            CREATE TABLE odoo_capture_selection_revision (
                selection_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                content_hash VARCHAR NOT NULL UNIQUE,
                model VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                created_by VARCHAR NOT NULL,
                selection_json VARCHAR NOT NULL,
                PRIMARY KEY (selection_id, version)
            );

            CREATE TABLE odoo_capture_selection_current (
                model VARCHAR PRIMARY KEY,
                selection_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE odoo_capture_manifest_revision (
                manifest_id VARCHAR PRIMARY KEY,
                content_hash VARCHAR NOT NULL UNIQUE,
                selection_hash VARCHAR NOT NULL,
                dataset_id VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                data_storage_key VARCHAR NOT NULL,
                data_size_bytes BIGINT NOT NULL,
                provenance_size_bytes BIGINT NOT NULL,
                provenance_storage_key VARCHAR NOT NULL UNIQUE,
                retention_until VARCHAR NOT NULL,
                captured_at VARCHAR NOT NULL,
                manifest_json VARCHAR NOT NULL
            );

            CREATE TABLE odoo_capture_manifest_current (
                dataset_id VARCHAR PRIMARY KEY,
                manifest_id VARCHAR NOT NULL
            );

            CREATE TABLE odoo_schema_catalog (
                singleton_id INTEGER PRIMARY KEY,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE derived_entity_plan_revision (
                plan_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                updated_by VARCHAR NOT NULL,
                plan_json VARCHAR NOT NULL,
                PRIMARY KEY (plan_id, version)
            );

            CREATE TABLE derived_entity_plan_current (
                singleton_id INTEGER PRIMARY KEY,
                plan_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE odoo_model_catalog (
                singleton_id INTEGER PRIMARY KEY,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE mapping_working_draft (
                singleton_id INTEGER PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                draft_json VARCHAR NOT NULL
            );

            CREATE TABLE mapping_mutation_receipt (
                operation_id VARCHAR PRIMARY KEY,
                action VARCHAR NOT NULL,
                request_hash VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                submitted_working_draft_version INTEGER,
                submitted_mapping_revision_version INTEGER,
                working_draft_version INTEGER,
                mapping_revision_version INTEGER,
                content_identity VARCHAR NOT NULL,
                failure_code VARCHAR NOT NULL,
                failure_detail VARCHAR NOT NULL,
                started_at VARCHAR NOT NULL,
                completed_at VARCHAR,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL
            );

            CREATE TABLE retired_evidence (
                evidence_type VARCHAR NOT NULL,
                evidence_key VARCHAR NOT NULL,
                retired_at VARCHAR NOT NULL,
                retirement_reason VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                PRIMARY KEY (evidence_type, evidence_key)
            );

            CREATE TABLE schema_governance_revision (
                governance_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                catalog_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                governance_json VARCHAR NOT NULL,
                PRIMARY KEY (governance_id, version)
            );

            CREATE TABLE schema_governance_current (
                singleton_id INTEGER PRIMARY KEY,
                governance_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE mapping_revision (
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                parent_version INTEGER,
                content_hash VARCHAR NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                revision_json VARCHAR NOT NULL,
                PRIMARY KEY (mapping_id, version)
            );

            CREATE TABLE mapping_current (
                singleton_id INTEGER PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE mapping_validation (
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                validator_version VARCHAR NOT NULL,
                validation_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                validation_json VARCHAR NOT NULL,
                PRIMARY KEY (mapping_id, version, validation_hash)
            );

            CREATE TABLE mapping_submission (
                submission_id VARCHAR PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                content_hash VARCHAR NOT NULL,
                validation_hash VARCHAR NOT NULL,
                submitted_at VARCHAR NOT NULL,
                submission_json VARCHAR NOT NULL
            );

            CREATE TABLE canonical_staging_run (
                run_id VARCHAR PRIMARY KEY,
                content_hash VARCHAR NOT NULL,
                mapping_id VARCHAR NOT NULL,
                mapping_version INTEGER NOT NULL,
                physical_selection_hash VARCHAR NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                mapping_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                derived_plan_hash VARCHAR,
                compiled_plan_hash VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                evaluator_version INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                published_at VARCHAR NOT NULL,
                published_by VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                run_issues_json VARCHAR NOT NULL,
                reconciliation_json VARCHAR NOT NULL,
                dataset_reconciliation_json VARCHAR NOT NULL,
                control_totals_json VARCHAR NOT NULL,
                retired_at VARCHAR,
                retired_reason VARCHAR,
                successor_run_id VARCHAR
            );

            CREATE TABLE canonical_staging_row (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                row_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                source_row BIGINT NOT NULL,
                target_model VARCHAR NOT NULL,
                disposition VARCHAR NOT NULL,
                record_label VARCHAR NOT NULL DEFAULT '',
                quality_identity_key VARCHAR,
                row_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, row_id)
            );

            CREATE INDEX canonical_staging_row_lookup
                ON canonical_staging_row (run_id, dataset, disposition);

            CREATE TABLE canonical_staging_current (
                singleton_id INTEGER PRIMARY KEY,
                run_id VARCHAR NOT NULL
            );

            CREATE TABLE quality_ruleset_revision (
                ruleset_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                parent_version INTEGER,
                mapping_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                created_at VARCHAR NOT NULL,
                created_by VARCHAR NOT NULL,
                ruleset_json VARCHAR NOT NULL,
                PRIMARY KEY (ruleset_id, version)
            );

            CREATE TABLE quality_ruleset_current (
                singleton_id INTEGER PRIMARY KEY,
                ruleset_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE quality_run (
                run_id VARCHAR PRIMARY KEY,
                content_hash VARCHAR NOT NULL,
                staging_run_id VARCHAR NOT NULL,
                staging_content_hash VARCHAR NOT NULL,
                ruleset_hash VARCHAR NOT NULL,
                mapping_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                retention_context_hash VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                evaluator_version INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                published_at VARCHAR NOT NULL,
                published_by VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                source_count BIGINT NOT NULL,
                issue_count BIGINT NOT NULL,
                quarantine_count BIGINT NOT NULL,
                summary_json VARCHAR NOT NULL,
                effective_dataset_run_id VARCHAR,
                effective_dataset_hash VARCHAR,
                retired_at VARCHAR,
                retired_reason VARCHAR,
                successor_run_id VARCHAR
            );

            CREATE TABLE quality_row_result (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                row_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                source_row BIGINT NOT NULL,
                record_label VARCHAR NOT NULL DEFAULT '',
                base_disposition VARCHAR NOT NULL DEFAULT 'CANDIDATE',
                effective_disposition VARCHAR NOT NULL,
                requires_review BOOLEAN NOT NULL,
                row_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, row_id)
            );

            CREATE INDEX quality_row_result_lookup
                ON quality_row_result (
                    run_id, effective_disposition, requires_review, dataset
                );

            CREATE TABLE quality_evidence_projection (
                run_id VARCHAR PRIMARY KEY,
                contract_version INTEGER NOT NULL,
                projection_json VARCHAR NOT NULL
            );

            CREATE TABLE quality_issue (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                issue_id VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                row_id VARCHAR,
                policy VARCHAR NOT NULL,
                issue_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, issue_id)
            );

            CREATE INDEX quality_issue_lookup
                ON quality_issue (run_id, dataset, policy, row_id);

            CREATE TABLE source_accounting_entry (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                physical_dataset_id VARCHAR NOT NULL,
                source_row BIGINT NOT NULL,
                state VARCHAR NOT NULL,
                entry_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, physical_dataset_id, source_row)
            );

            CREATE TABLE source_accounting_link (
                run_id VARCHAR NOT NULL,
                accounting_ordinal BIGINT NOT NULL,
                row_id VARCHAR NOT NULL,
                PRIMARY KEY (run_id, accounting_ordinal, row_id)
            );

            CREATE INDEX source_accounting_link_lookup
                ON source_accounting_link (run_id, row_id);

            CREATE TABLE quality_quarantine_entry (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                entry_id VARCHAR NOT NULL,
                row_id VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                entry_json VARCHAR NOT NULL,
                superseded_by_run_id VARCHAR,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, entry_id)
            );

            CREATE INDEX quality_quarantine_lookup
                ON quality_quarantine_entry (run_id, row_id, rule_id);

            CREATE TABLE quality_current (
                singleton_id INTEGER PRIMARY KEY,
                run_id VARCHAR NOT NULL
            );

            CREATE TABLE normalization_run (
                run_id VARCHAR PRIMARY KEY,
                content_hash VARCHAR NOT NULL,
                staging_run_id VARCHAR NOT NULL,
                staging_content_hash VARCHAR NOT NULL,
                quality_run_id VARCHAR NOT NULL,
                quality_content_hash VARCHAR NOT NULL,
                mapping_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                policy_hash VARCHAR NOT NULL,
                retention_context_hash VARCHAR NOT NULL,
                eligible_dataset_hash VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                evaluator_version INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                lifecycle_version INTEGER NOT NULL,
                published_at VARCHAR NOT NULL,
                published_by VARCHAR NOT NULL,
                eligible_record_count BIGINT NOT NULL,
                changed_record_count BIGINT NOT NULL,
                automatic_group_count BIGINT NOT NULL,
                decision_group_count BIGINT NOT NULL,
                set_aside_record_count BIGINT NOT NULL,
                evaluation_json VARCHAR NOT NULL,
                dry_run_json VARCHAR NOT NULL,
                effective_dataset_run_id VARCHAR,
                effective_dataset_hash VARCHAR,
                retired_at VARCHAR,
                retired_reason VARCHAR,
                successor_run_id VARCHAR
            );

            CREATE TABLE normalization_effect (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                effect_id VARCHAR NOT NULL,
                group_id VARCHAR NOT NULL,
                row_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                source_row BIGINT NOT NULL,
                target_field VARCHAR NOT NULL,
                eligible BOOLEAN NOT NULL,
                effect_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, effect_id)
            );

            CREATE INDEX normalization_effect_lookup
                ON normalization_effect (
                    run_id, group_id, eligible, dataset, source_row
                );

            CREATE TABLE normalization_group (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                group_id VARCHAR NOT NULL,
                kind VARCHAR NOT NULL,
                outcome VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                target_field VARCHAR NOT NULL,
                requires_decision BOOLEAN NOT NULL,
                group_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, group_id)
            );

            CREATE INDEX normalization_group_lookup
                ON normalization_group (
                    run_id, requires_decision, dataset, target_field
                );

            CREATE TABLE normalization_transition (
                run_id VARCHAR NOT NULL,
                lifecycle_version INTEGER NOT NULL,
                event_type VARCHAR NOT NULL,
                occurred_at VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                state_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, lifecycle_version)
            );

            CREATE TABLE normalization_current (
                singleton_id INTEGER PRIMARY KEY,
                run_id VARCHAR NOT NULL
            );

            CREATE TABLE transformation_impact_run (
                singleton_id INTEGER PRIMARY KEY,
                identity_hash VARCHAR NOT NULL,
                physical_selection_hash VARCHAR NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                mapping_content_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                derived_plan_hash VARCHAR,
                contract_version INTEGER NOT NULL,
                evaluator_version INTEGER NOT NULL,
                created_at VARCHAR NOT NULL,
                created_by VARCHAR NOT NULL,
                affected_row_count BIGINT NOT NULL,
                evaluated_count BIGINT NOT NULL,
                changed_count BIGINT NOT NULL,
                fallback_count BIGINT NOT NULL,
                null_count BIGINT NOT NULL,
                invalid_count BIGINT NOT NULL,
                provided_count BIGINT NOT NULL,
                unchanged_count BIGINT NOT NULL
            );

            CREATE TABLE transformation_impact_row (
                ordinal BIGINT PRIMARY KEY,
                dataset VARCHAR NOT NULL,
                source_row BIGINT NOT NULL,
                source_column VARCHAR NOT NULL,
                target_field VARCHAR NOT NULL,
                raw_value VARCHAR NOT NULL,
                proposed_value VARCHAR NOT NULL,
                rules VARCHAR NOT NULL,
                outcome VARCHAR NOT NULL,
                message VARCHAR NOT NULL
            );

            CREATE INDEX transformation_impact_row_lookup
                ON transformation_impact_row (
                    dataset, outcome, target_field, ordinal
                );

            CREATE TABLE transformation_rule_impact (
                rule_fingerprint VARCHAR PRIMARY KEY,
                dataset_id VARCHAR NOT NULL,
                target_field VARCHAR NOT NULL,
                rule_kind VARCHAR NOT NULL,
                evaluated_value_count BIGINT NOT NULL,
                matched_value_count BIGINT NOT NULL,
                changed_value_count BIGINT NOT NULL
            );

            CREATE TABLE transformation_rule_acknowledgement (
                identity_hash VARCHAR NOT NULL,
                rule_fingerprint VARCHAR NOT NULL,
                acknowledged_at VARCHAR NOT NULL,
                acknowledged_by VARCHAR NOT NULL,
                PRIMARY KEY (identity_hash, rule_fingerprint)
            );

            CREATE TABLE readiness_run (
                run_id VARCHAR PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                mapping_version INTEGER NOT NULL,
                mapping_content_hash VARCHAR NOT NULL,
                target_hash VARCHAR NOT NULL,
                staging_run_id VARCHAR NOT NULL,
                staging_content_hash VARCHAR NOT NULL,
                quality_run_id VARCHAR NOT NULL,
                quality_content_hash VARCHAR NOT NULL,
                checked_at VARCHAR NOT NULL,
                checked_by VARCHAR NOT NULL,
                report_json VARCHAR NOT NULL,
                normalization_run_id VARCHAR DEFAULT '',
                normalization_content_hash VARCHAR DEFAULT '',
                normalization_lifecycle_version BIGINT DEFAULT 0,
                eligible_dataset_hash VARCHAR DEFAULT '',
                frozen_input_hash VARCHAR DEFAULT '',
                requirement_plan_hash VARCHAR DEFAULT '',
                metadata_snapshot_hash VARCHAR DEFAULT '',
                record_snapshot_hash VARCHAR DEFAULT '',
                result_hash VARCHAR DEFAULT '',
                manifest_hash VARCHAR DEFAULT ''
            );

            CREATE TABLE audit_event (
                event_id BIGINT PRIMARY KEY,
                event_type VARCHAR NOT NULL,
                workspace_revision INTEGER NOT NULL,
                occurred_at VARCHAR NOT NULL,
                detail VARCHAR NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL
            );

            CREATE SEQUENCE audit_event_sequence START 1;
            """
        )
        create_schema_migration_ledger(connection)
        create_preflight_schema(connection)
        create_advanced_coverage_schema(connection)
        create_preparation_session_schema(connection)
        create_execution_schema(connection)
        create_reconciliation_schema(connection)
        create_source_snapshot_schema(connection)
        create_prepared_snapshot_schema(connection)
        create_derived_value_artifact_schema(connection)
        create_recipe_compilation_schema(connection)
        create_supporting_lookup_schema(connection)

    def _ensure_workspace_database_schema(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        """Upgrade the recognized generation, then require its exact schema."""

        try:
            ensure_current_schema(
                connection,
                expected_generation=SCHEMA_GENERATION,
                baseline_version=SCHEMA_BASELINE_VERSION,
                target_version=SCHEMA_VERSION,
                upgrades=WORKSPACE_ENGINE_UPGRADES,
                validate_current=lambda: self._validate_current_workspace_schema(
                    connection
                ),
                compatibility_error=lambda: WorkspaceStateCompatibilityError(
                    _UNSUPPORTED_WORKSPACE_MESSAGE
                ),
            )
        except WorkspaceStateCompatibilityError:
            raise
        except duckdb.Error as error:
            raise WorkspaceStateCompatibilityError(
                _UNSUPPORTED_WORKSPACE_MESSAGE
            ) from error

    def _validate_current_workspace_schema(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        try:
            tables = frozenset(
                str(item[0])
                for item in connection.execute("SHOW TABLES").fetchall()
            )
        except duckdb.Error as error:
            raise WorkspaceStateCompatibilityError(
                _UNSUPPORTED_WORKSPACE_MESSAGE
            ) from error
        if tables != _WORKSPACE_ENGINE_TABLES:
            raise WorkspaceStateCompatibilityError(_UNSUPPORTED_WORKSPACE_MESSAGE)
        for table, expected in (
            ("schema_migration", _SCHEMA_MIGRATION_COLUMNS),
            ("workspace_projection_cache", _WORKSPACE_PROJECTION_COLUMNS),
            ("audit_event", _AUDIT_EVENT_COLUMNS),
            (
                "mapping_mutation_receipt",
                _MAPPING_MUTATION_RECEIPT_COLUMNS,
            ),
            (
                "odoo_capture_selection_current",
                _ODOO_CAPTURE_SELECTION_CURRENT_COLUMNS,
            ),
            (
                "odoo_capture_manifest_current",
                _ODOO_CAPTURE_MANIFEST_CURRENT_COLUMNS,
            ),
        ):
            try:
                columns = tuple(
                    str(item[1])
                    for item in connection.execute(
                        f"PRAGMA table_info('{table}')"
                    ).fetchall()
                )
            except duckdb.Error as error:
                raise WorkspaceStateCompatibilityError(
                    _UNSUPPORTED_WORKSPACE_MESSAGE
                ) from error
            if columns != expected:
                raise WorkspaceStateCompatibilityError(
                    _UNSUPPORTED_WORKSPACE_MESSAGE
                )


def _upgrade_workspace_engine_v1_to_v2(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    create_schema_migration_ledger(connection)


def _upgrade_workspace_engine_v2_to_v3(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    connection.execute(
        """
        CREATE TABLE mapping_mutation_receipt (
            operation_id VARCHAR PRIMARY KEY,
            action VARCHAR NOT NULL,
            request_hash VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            submitted_working_draft_version INTEGER,
            submitted_mapping_revision_version INTEGER,
            working_draft_version INTEGER,
            mapping_revision_version INTEGER,
            content_identity VARCHAR NOT NULL,
            failure_code VARCHAR NOT NULL,
            failure_detail VARCHAR NOT NULL,
            started_at VARCHAR NOT NULL,
            completed_at VARCHAR,
            actor_issuer VARCHAR NOT NULL,
            actor_subject VARCHAR NOT NULL
        )
        """
    )


def _upgrade_workspace_engine_v3_to_v4(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Turn the two Odoo capture singleton pointers into dataset-keyed sets."""

    connection.execute(
        """
        ALTER TABLE odoo_capture_selection_current
        RENAME TO odoo_capture_selection_current_v3;
        CREATE TABLE odoo_capture_selection_current (
            model VARCHAR PRIMARY KEY,
            selection_id VARCHAR NOT NULL,
            version INTEGER NOT NULL
        );
        INSERT INTO odoo_capture_selection_current
        SELECT revision.model, current_selection.selection_id,
               current_selection.version
          FROM odoo_capture_selection_current_v3 AS current_selection
          JOIN odoo_capture_selection_revision AS revision
            ON revision.selection_id = current_selection.selection_id
           AND revision.version = current_selection.version;
        DROP TABLE odoo_capture_selection_current_v3;

        ALTER TABLE odoo_capture_manifest_current
        RENAME TO odoo_capture_manifest_current_v3;
        CREATE TABLE odoo_capture_manifest_current (
            dataset_id VARCHAR PRIMARY KEY,
            manifest_id VARCHAR NOT NULL
        );
        INSERT INTO odoo_capture_manifest_current
        SELECT revision.dataset_id, current_manifest.manifest_id
          FROM odoo_capture_manifest_current_v3 AS current_manifest
          JOIN odoo_capture_manifest_revision AS revision
            ON revision.manifest_id = current_manifest.manifest_id;
        DROP TABLE odoo_capture_manifest_current_v3;
        """
    )


WORKSPACE_ENGINE_UPGRADES = {
    1: ForwardSchemaUpgrade(
        migration_id="workspace-engine-v1-to-v2-migration-ledger",
        apply=_upgrade_workspace_engine_v1_to_v2,
    ),
    2: ForwardSchemaUpgrade(
        migration_id="workspace-engine-v2-to-v3-mapping-mutation-receipts",
        apply=_upgrade_workspace_engine_v2_to_v3,
    ),
    3: ForwardSchemaUpgrade(
        migration_id="workspace-engine-v3-to-v4-odoo-capture-sets",
        apply=_upgrade_workspace_engine_v3_to_v4,
    ),
}
