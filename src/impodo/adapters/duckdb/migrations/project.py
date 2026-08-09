"""Creation and incremental migration of per-project DuckDB files."""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)

import duckdb

from ..constants import SCHEMA_VERSION
from .mapping_draft_retirement import retire_mapping_draft
from .preflight import create_preflight_schema
from .advanced_coverage import create_advanced_coverage_schema
from .preparation_session import create_preparation_session_schema
from .execution import create_execution_schema
from .reconciliation import create_reconciliation_schema
from .source_snapshot import create_source_snapshot_schema

class ProjectMigrationsMixin:
    """Keep new databases and upgrades on one monotonic schema path.

    Initialization creates the current schema; migration reads the stored
    version and applies every intermediate upgrade in order within one
    transaction. Semantic retirements preserve incompatible legacy payloads
    as retired evidence instead of silently treating them as current.
    """

    def _initialize_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES ({SCHEMA_VERSION});

            CREATE TABLE project (
                project_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                source_system VARCHAR NOT NULL,
                export_status VARCHAR NOT NULL,
                export_date VARCHAR,
                description VARCHAR NOT NULL,
                data_manager VARCHAR NOT NULL,
                functional_owner VARCHAR NOT NULL,
                business_unit VARCHAR NOT NULL,
                data_classification VARCHAR NOT NULL,
                retention_days INTEGER NOT NULL,
                support_access BOOLEAN NOT NULL,
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
                report_json VARCHAR NOT NULL
            );

            CREATE TABLE audit_event (
                event_id BIGINT PRIMARY KEY,
                event_type VARCHAR NOT NULL,
                project_revision INTEGER NOT NULL,
                occurred_at VARCHAR NOT NULL,
                detail VARCHAR NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL
            );

            CREATE SEQUENCE audit_event_sequence START 1;
            """
        )
        create_preflight_schema(connection)
        create_advanced_coverage_schema(connection)
        create_preparation_session_schema(connection)
        create_execution_schema(connection)
        create_reconciliation_schema(connection)
        create_source_snapshot_schema(connection)

    def _migrate_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        row = connection.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            raise RuntimeError("Project database has no schema version")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "Project database was created by a newer Impodo version"
            )
        if version < SCHEMA_VERSION:
            connection.begin()
            try:
                if version == 1:
                    connection.execute(
                        """
                        ALTER TABLE project
                        ADD COLUMN odoo_connection_mode VARCHAR DEFAULT 'REMOTE'
                        """
                    )
                    version = 2
                if version == 2:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_catalog (
                            file_id VARCHAR PRIMARY KEY,
                            source_sha256 VARCHAR NOT NULL,
                            contract_version INTEGER NOT NULL,
                            inspected_at VARCHAR NOT NULL,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 3
                if version == 3:
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_issuer
                        VARCHAR DEFAULT 'urn:impodo:legacy'
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_subject
                        VARCHAR DEFAULT 'unknown'
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_display_name
                        VARCHAR DEFAULT 'Legacy operator'
                        """
                    )
                    version = 4
                if version == 4:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_configuration (
                            file_id VARCHAR PRIMARY KEY,
                            source_sha256 VARCHAR NOT NULL,
                            catalog_hash VARCHAR NOT NULL,
                            configuration_json VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_selection (
                            singleton_id INTEGER PRIMARY KEY,
                            selection_json VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS odoo_schema_catalog (
                            singleton_id INTEGER PRIMARY KEY,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_draft (
                            singleton_id INTEGER PRIMARY KEY,
                            draft_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 5
                if version == 5:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_governance_revision (
                            governance_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            catalog_hash VARCHAR NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            governance_json VARCHAR NOT NULL,
                            PRIMARY KEY (governance_id, version)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_governance_current (
                            singleton_id INTEGER PRIMARY KEY,
                            governance_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_revision (
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            parent_version INTEGER,
                            content_hash VARCHAR NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            schema_hash VARCHAR NOT NULL,
                            created_at VARCHAR NOT NULL,
                            revision_json VARCHAR NOT NULL,
                            PRIMARY KEY (mapping_id, version)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_current (
                            singleton_id INTEGER PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_validation (
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            validator_version VARCHAR NOT NULL,
                            validation_hash VARCHAR NOT NULL,
                            created_at VARCHAR NOT NULL,
                            validation_json VARCHAR NOT NULL,
                            PRIMARY KEY (mapping_id, version, validation_hash)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_submission (
                            submission_id VARCHAR PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            validation_hash VARCHAR NOT NULL,
                            submitted_at VARCHAR NOT NULL,
                            submission_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 6
                if version == 6:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS odoo_model_catalog (
                            singleton_id INTEGER PRIMARY KEY,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 7
                if version == 7:
                    legacy_target_column = "_".join(("target", "environment"))
                    drop_legacy_column = (
                        "ALTER TABLE project DROP COLUMN IF EXISTS "
                        f'"{legacy_target_column}"'
                    )
                    connection.execute(drop_legacy_column)
                    available_tables = {
                        str(row[0])
                        for row in connection.execute("SHOW TABLES").fetchall()
                    }
                    for table in (
                        "odoo_model_catalog",
                        "odoo_schema_catalog",
                        "schema_governance_current",
                        "schema_governance_revision",
                        "mapping_draft",
                        "mapping_current",
                        "mapping_revision",
                        "mapping_validation",
                        "mapping_submission",
                    ):
                        if table in available_tables:
                            connection.execute(f"DELETE FROM {table}")
                    connection.execute(
                        """
                        UPDATE project
                           SET mapping_version = NULL,
                               current_run_id = NULL,
                               approval_status = 'INVALIDATED'
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO audit_event (
                            event_id, event_type, project_revision, occurred_at,
                            detail, actor_issuer, actor_subject,
                            actor_display_name
                        )
                        SELECT nextval('audit_event_sequence'),
                               'TARGET_CONTRACT_MIGRATED', revision, updated_at,
                               'Target-derived evidence invalidated after '
                               || 'the target contract changed',
                               'urn:impodo:migration', 'schema-v8',
                               'Impodo schema migration'
                          FROM project
                        """
                    )
                    version = 8
                if version == 8:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS derived_entity_plan_revision (
                            plan_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            updated_at VARCHAR NOT NULL,
                            updated_by VARCHAR NOT NULL,
                            plan_json VARCHAR NOT NULL,
                            PRIMARY KEY (plan_id, version)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS derived_entity_plan_current (
                            singleton_id INTEGER PRIMARY KEY,
                            plan_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    version = 9
                if version == 9:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS readiness_run (
                            run_id VARCHAR PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            mapping_version INTEGER NOT NULL,
                            mapping_content_hash VARCHAR NOT NULL,
                            target_hash VARCHAR NOT NULL,
                            checked_at VARCHAR NOT NULL,
                            checked_by VARCHAR NOT NULL,
                            report_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 10
                if version == 10:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_working_draft (
                            singleton_id INTEGER PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            schema_hash VARCHAR NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            updated_at VARCHAR NOT NULL,
                            draft_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 11
                if version == 11:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS canonical_staging_run (
                            run_id VARCHAR PRIMARY KEY,
                            content_hash VARCHAR NOT NULL,
                            mapping_id VARCHAR NOT NULL,
                            mapping_version INTEGER NOT NULL,
                            physical_selection_hash VARCHAR NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            mapping_hash VARCHAR NOT NULL,
                            schema_hash VARCHAR NOT NULL,
                            derived_plan_hash VARCHAR,
                            contract_version INTEGER NOT NULL,
                            evaluator_version INTEGER NOT NULL,
                            status VARCHAR NOT NULL,
                            published_at VARCHAR NOT NULL,
                            published_by VARCHAR NOT NULL,
                            row_count BIGINT NOT NULL,
                            run_issues_json VARCHAR NOT NULL,
                            reconciliation_json VARCHAR NOT NULL,
                            dataset_reconciliation_json VARCHAR NOT NULL,
                            retired_at VARCHAR,
                            retired_reason VARCHAR,
                            successor_run_id VARCHAR
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS canonical_staging_row (
                            run_id VARCHAR NOT NULL,
                            ordinal BIGINT NOT NULL,
                            row_id VARCHAR NOT NULL,
                            dataset VARCHAR NOT NULL,
                            source_row BIGINT NOT NULL,
                            target_model VARCHAR NOT NULL,
                            disposition VARCHAR NOT NULL,
                            row_json VARCHAR NOT NULL,
                            PRIMARY KEY (run_id, ordinal),
                            UNIQUE (run_id, row_id)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS canonical_staging_row_lookup
                            ON canonical_staging_row (
                                run_id, dataset, disposition
                            )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS canonical_staging_current (
                            singleton_id INTEGER PRIMARY KEY,
                            run_id VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE readiness_run
                        ADD COLUMN IF NOT EXISTS staging_run_id
                        VARCHAR DEFAULT ''
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE readiness_run
                        ADD COLUMN IF NOT EXISTS staging_content_hash
                        VARCHAR DEFAULT ''
                        """
                    )
                    version = 12
                if version == 12:
                    connection.execute(
                        """
                        ALTER TABLE canonical_staging_run
                        ADD COLUMN IF NOT EXISTS control_totals_json
                        VARCHAR DEFAULT '[]'
                        """
                    )
                    connection.execute(
                        """
                        UPDATE canonical_staging_run
                           SET status = 'INVALIDATED',
                               retired_at = COALESCE(retired_at, ?),
                               retired_reason = COALESCE(
                                   retired_reason,
                                   'STAGING_CONTRACT_UPGRADED'
                               )
                         WHERE status = 'PUBLISHED'
                        """,
                        [datetime.now(timezone.utc).isoformat()],
                    )
                    connection.execute("DELETE FROM canonical_staging_current")
                    version = 13
                if version == 13:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS transformation_impact_run (
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
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS transformation_impact_row (
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
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS transformation_impact_row_lookup
                            ON transformation_impact_row (
                                dataset, outcome, target_field, ordinal
                            )
                        """
                    )
                    version = 14
                if version == 14:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_ruleset_revision (
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
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_ruleset_current (
                            singleton_id INTEGER PRIMARY KEY,
                            ruleset_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_run (
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
                            retired_at VARCHAR,
                            retired_reason VARCHAR,
                            successor_run_id VARCHAR
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_row_result (
                            run_id VARCHAR NOT NULL,
                            ordinal BIGINT NOT NULL,
                            row_id VARCHAR NOT NULL,
                            dataset VARCHAR NOT NULL,
                            source_row BIGINT NOT NULL,
                            effective_disposition VARCHAR NOT NULL,
                            requires_review BOOLEAN NOT NULL,
                            row_json VARCHAR NOT NULL,
                            PRIMARY KEY (run_id, ordinal),
                            UNIQUE (run_id, row_id)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS quality_row_result_lookup
                            ON quality_row_result (
                                run_id, effective_disposition,
                                requires_review, dataset
                            )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_issue (
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
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS quality_issue_lookup
                            ON quality_issue (run_id, dataset, policy, row_id)
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_accounting_entry (
                            run_id VARCHAR NOT NULL,
                            ordinal BIGINT NOT NULL,
                            physical_dataset_id VARCHAR NOT NULL,
                            source_row BIGINT NOT NULL,
                            state VARCHAR NOT NULL,
                            entry_json VARCHAR NOT NULL,
                            PRIMARY KEY (run_id, ordinal),
                            UNIQUE (run_id, physical_dataset_id, source_row)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_accounting_link (
                            run_id VARCHAR NOT NULL,
                            accounting_ordinal BIGINT NOT NULL,
                            row_id VARCHAR NOT NULL,
                            PRIMARY KEY (run_id, accounting_ordinal, row_id)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS source_accounting_link_lookup
                            ON source_accounting_link (run_id, row_id)
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_quarantine_entry (
                            run_id VARCHAR NOT NULL,
                            ordinal BIGINT NOT NULL,
                            entry_id VARCHAR NOT NULL,
                            row_id VARCHAR NOT NULL,
                            rule_id VARCHAR NOT NULL,
                            entry_json VARCHAR NOT NULL,
                            superseded_by_run_id VARCHAR,
                            PRIMARY KEY (run_id, ordinal),
                            UNIQUE (run_id, entry_id)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS quality_quarantine_lookup
                            ON quality_quarantine_entry (run_id, row_id, rule_id)
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quality_current (
                            singleton_id INTEGER PRIMARY KEY,
                            run_id VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE readiness_run
                        ADD COLUMN IF NOT EXISTS quality_run_id
                        VARCHAR DEFAULT ''
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE readiness_run
                        ADD COLUMN IF NOT EXISTS quality_content_hash
                        VARCHAR DEFAULT ''
                        """
                    )
                    connection.execute(
                        """
                        UPDATE project
                           SET current_run_id = NULL,
                               approval_status = 'INVALIDATED'
                        """
                    )
                    version = 15
                if version == 15:
                    self._create_normalization_tables(connection)
                    version = 16
                if version == 16:
                    retire_mapping_draft(connection)
                    version = 17
                if version == 17:
                    create_preflight_schema(connection)
                    version = 18
                if version == 18:
                    connection.execute(
                        """
                        ALTER TABLE canonical_staging_run
                        ADD COLUMN IF NOT EXISTS compiled_plan_hash VARCHAR
                        """
                    )
                    connection.execute(
                        """
                        UPDATE canonical_staging_run
                           SET status = 'INVALIDATED',
                               retired_at = COALESCE(retired_at, ?),
                               retired_reason = COALESCE(
                                   retired_reason,
                                   'COMPILED_PLAN_REQUIRED'
                               )
                         WHERE status = 'PUBLISHED'
                        """,
                        [datetime.now(timezone.utc).isoformat()],
                    )
                    connection.execute("DELETE FROM canonical_staging_current")
                    version = 19
                if version == 19:
                    create_advanced_coverage_schema(connection)
                    connection.execute(
                        """
                        UPDATE canonical_staging_run
                           SET status = 'INVALIDATED',
                               retired_at = COALESCE(retired_at, ?),
                               retired_reason = COALESCE(
                                   retired_reason,
                                   'MULTI_SOURCE_LINEAGE_REQUIRED'
                               )
                         WHERE status = 'PUBLISHED'
                        """,
                        [datetime.now(timezone.utc).isoformat()],
                    )
                    connection.execute("DELETE FROM canonical_staging_current")
                    connection.execute("DELETE FROM quality_current")
                    connection.execute("DELETE FROM normalization_current")
                    connection.execute("DELETE FROM preflight_current")
                    version = 20
                if version == 20:
                    create_preparation_session_schema(connection)
                    version = 21
                if version == 21:
                    create_preparation_session_schema(connection)
                    create_advanced_coverage_schema(connection)
                    version = 22
                if version == 22:
                    create_preparation_session_schema(connection)
                    for table in (
                        "preparation_final_row",
                        "preparation_impact_row",
                        "preparation_physical_row",
                        "preparation_lineage",
                        "preparation_finalization_row",
                        "preparation_identity_group",
                        "preparation_provisional_row",
                    ):
                        connection.execute(f"DELETE FROM {table}")
                    connection.execute(
                        """
                        UPDATE preparation_session
                           SET status = 'FAILED',
                               failure_code = 'SESSION_SCHEMA_UPGRADED',
                               provisional_row_count = 0,
                               canonical_row_count = 0,
                               impact_row_count = 0
                         WHERE status IN ('BUILDING', 'FINALIZING', 'READY')
                        """
                    )
                    version = 23
                if version == 23:
                    create_execution_schema(connection)
                    version = 24
                if version == 24:
                    create_reconciliation_schema(connection)
                    version = 25
                if version == 25:
                    create_preparation_session_schema(connection)
                    version = 26
                if version == 26:
                    create_preparation_session_schema(connection)
                    version = 27
                if version == 27:
                    create_source_snapshot_schema(connection)
                    version = 28
                connection.execute(
                    "UPDATE schema_version SET version = ?",
                    [version],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _create_normalization_tables(
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        """Add the Slice 4 evidence boundary without changing older evidence."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS normalization_run (
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
                retired_at VARCHAR,
                retired_reason VARCHAR,
                successor_run_id VARCHAR
            );

            CREATE TABLE IF NOT EXISTS normalization_effect (
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

            CREATE INDEX IF NOT EXISTS normalization_effect_lookup
                ON normalization_effect (
                    run_id, group_id, eligible, dataset, source_row
                );

            CREATE TABLE IF NOT EXISTS normalization_group (
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

            CREATE INDEX IF NOT EXISTS normalization_group_lookup
                ON normalization_group (
                    run_id, requires_decision, dataset, target_field
                );

            CREATE TABLE IF NOT EXISTS normalization_transition (
                run_id VARCHAR NOT NULL,
                lifecycle_version INTEGER NOT NULL,
                event_type VARCHAR NOT NULL,
                occurred_at VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                state_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, lifecycle_version)
            );

            CREATE TABLE IF NOT EXISTS normalization_current (
                singleton_id INTEGER PRIMARY KEY,
                run_id VARCHAR NOT NULL
            );
            """
        )
