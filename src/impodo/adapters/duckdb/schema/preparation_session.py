"""Temporary durable storage for bounded Stage-E preparation sessions."""

from __future__ import annotations

import duckdb


def create_preparation_session_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create unpublished row, lineage, impact, and finalized-row relations."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS preparation_session (
            session_id VARCHAR PRIMARY KEY,
            status VARCHAR NOT NULL,
            mapping_id VARCHAR NOT NULL,
            mapping_version BIGINT NOT NULL,
            physical_selection_hash VARCHAR NOT NULL,
            source_selection_hash VARCHAR NOT NULL,
            mapping_hash VARCHAR NOT NULL,
            schema_hash VARCHAR NOT NULL,
            derived_plan_hash VARCHAR,
            compiled_plan_hash VARCHAR NOT NULL,
            contract_version INTEGER NOT NULL,
            evaluator_version INTEGER NOT NULL,
            source_hashes_json VARCHAR NOT NULL,
            run_issues_json VARCHAR NOT NULL,
            control_totals_json VARCHAR NOT NULL,
            reconciliation_json VARCHAR,
            dataset_reconciliation_json VARCHAR,
            impact_report_json VARCHAR,
            provisional_row_count BIGINT NOT NULL,
            canonical_row_count BIGINT NOT NULL,
            impact_row_count BIGINT NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL,
            failure_code VARCHAR
        );

        CREATE TABLE IF NOT EXISTS preparation_provisional_row (
            session_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            target_model VARCHAR NOT NULL,
            identity_hash VARCHAR NOT NULL,
            payload_kind VARCHAR NOT NULL,
            row_id VARCHAR,
            disposition VARCHAR,
            record_json VARCHAR NOT NULL,
            PRIMARY KEY (session_id, dataset, source_row)
        );

        CREATE INDEX IF NOT EXISTS preparation_identity_lookup
            ON preparation_provisional_row (
                session_id, dataset, identity_hash, source_row
            );

        CREATE TABLE IF NOT EXISTS preparation_identity_group (
            session_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            identity_hash VARCHAR NOT NULL,
            identity_count BIGINT NOT NULL,
            PRIMARY KEY (session_id, dataset, identity_hash)
        );

        CREATE TABLE IF NOT EXISTS preparation_finalization_row (
            session_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            payload_kind VARCHAR NOT NULL,
            record_json VARCHAR NOT NULL,
            identity_count BIGINT NOT NULL,
            physical_dataset_ids_json VARCHAR NOT NULL,
            physical_source_rows_json VARCHAR NOT NULL,
            PRIMARY KEY (session_id, ordinal)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS preparation_provisional_ordinal
            ON preparation_provisional_row (session_id, ordinal);

        CREATE TABLE IF NOT EXISTS preparation_lineage (
            session_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            output_source_row BIGINT NOT NULL,
            physical_dataset_id VARCHAR NOT NULL,
            physical_source_row BIGINT NOT NULL,
            PRIMARY KEY (
                session_id, dataset, output_source_row,
                physical_dataset_id, physical_source_row
            )
        );

        CREATE INDEX IF NOT EXISTS preparation_lineage_output_lookup
            ON preparation_lineage (
                session_id, dataset, output_source_row,
                physical_dataset_id, physical_source_row
            );

        CREATE TABLE IF NOT EXISTS preparation_physical_row (
            session_id VARCHAR NOT NULL,
            physical_dataset_id VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            PRIMARY KEY (session_id, physical_dataset_id, source_row)
        );

        CREATE TABLE IF NOT EXISTS preparation_impact_row (
            session_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            target_field VARCHAR NOT NULL,
            outcome VARCHAR NOT NULL,
            impact_json VARCHAR NOT NULL,
            PRIMARY KEY (session_id, ordinal)
        );

        CREATE INDEX IF NOT EXISTS preparation_impact_lookup
            ON preparation_impact_row (
                session_id, dataset, source_row, target_field, ordinal
            );

        CREATE TABLE IF NOT EXISTS preparation_normalization_group_seed (
            session_id VARCHAR NOT NULL,
            group_id VARCHAR NOT NULL,
            metadata_hash VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            target_field VARCHAR NOT NULL,
            rule_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            outcome VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            explanation VARCHAR NOT NULL,
            owner_label VARCHAR NOT NULL,
            PRIMARY KEY (session_id, group_id, metadata_hash)
        );

        CREATE INDEX IF NOT EXISTS preparation_normalization_group_seed_lookup
            ON preparation_normalization_group_seed (
                session_id, group_id
            );

        CREATE TABLE IF NOT EXISTS preparation_normalization_finding (
            session_id VARCHAR NOT NULL,
            issue_id VARCHAR NOT NULL,
            group_id VARCHAR NOT NULL,
            row_id VARCHAR NOT NULL,
            rule_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            outcome VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            target_field VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            explanation VARCHAR NOT NULL,
            owner_label VARCHAR NOT NULL,
            PRIMARY KEY (session_id, group_id, row_id)
        );

        CREATE INDEX IF NOT EXISTS preparation_normalization_finding_lookup
            ON preparation_normalization_finding (
                session_id, group_id, issue_id
            );

        CREATE TABLE IF NOT EXISTS preparation_final_row (
            session_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            row_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            target_model VARCHAR NOT NULL,
            disposition VARCHAR NOT NULL,
            row_json VARCHAR NOT NULL,
            PRIMARY KEY (session_id, ordinal),
            UNIQUE (session_id, row_id)
        );

        CREATE INDEX IF NOT EXISTS preparation_final_row_lookup
            ON preparation_final_row (
                session_id, dataset, source_row, row_id
            );

        CREATE TABLE IF NOT EXISTS preparation_direct_identity (
            session_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            dataset VARCHAR NOT NULL,
            identity_hash VARCHAR NOT NULL,
            base_disposition VARCHAR NOT NULL,
            finalized_duplicate BOOLEAN NOT NULL,
            PRIMARY KEY (session_id, ordinal)
        );

        CREATE INDEX IF NOT EXISTS preparation_direct_identity_lookup
            ON preparation_direct_identity (
                session_id, dataset, identity_hash
            );

        CREATE TABLE IF NOT EXISTS preparation_relationship_edge (
            session_id VARCHAR NOT NULL,
            child_ordinal BIGINT NOT NULL,
            child_row_id VARCHAR NOT NULL,
            child_dataset VARCHAR NOT NULL,
            child_source_row BIGINT NOT NULL,
            target_field VARCHAR NOT NULL,
            item_ordinal INTEGER NOT NULL,
            parent_dataset VARCHAR NOT NULL,
            normalized_key_json VARCHAR NOT NULL,
            parent_identity_hash VARCHAR NOT NULL,
            match_state VARCHAR NOT NULL CHECK (
                match_state IN ('PENDING', 'MISSING', 'UNIQUE', 'DUPLICATE')
            ),
            resolution_state VARCHAR NOT NULL CHECK (
                resolution_state IN (
                    'PENDING', 'MISSING', 'AMBIGUOUS',
                    'RESOLVED', 'UNSAFE_PARENT'
                )
            ),
            match_count BIGINT NOT NULL CHECK (match_count >= 0),
            resolved_parent_row_id VARCHAR,
            PRIMARY KEY (
                session_id, child_ordinal, target_field, item_ordinal
            )
        );

        CREATE INDEX IF NOT EXISTS preparation_relationship_parent_lookup
            ON preparation_relationship_edge (
                session_id, parent_identity_hash, resolved_parent_row_id
            );

        CREATE INDEX IF NOT EXISTS preparation_relationship_child_lookup
            ON preparation_relationship_edge (
                session_id, child_row_id, resolution_state
            );

        CREATE TABLE IF NOT EXISTS canonical_staging_row_issue (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            issue_ordinal INTEGER NOT NULL,
            issue_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal, issue_ordinal)
        );
        """
    )
