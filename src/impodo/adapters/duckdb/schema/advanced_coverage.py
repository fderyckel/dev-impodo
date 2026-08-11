"""DuckDB schema for scoped coverage and reviewed resolution."""

from __future__ import annotations

import duckdb


def create_advanced_coverage_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create immutable Slice 6 evidence relations and current pointers."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_scope_revision (
            scope_id VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            content_hash VARCHAR NOT NULL,
            source_selection_hash VARCHAR NOT NULL,
            scope_json VARCHAR NOT NULL,
            PRIMARY KEY (scope_id, version)
        );

        CREATE TABLE IF NOT EXISTS coverage_scope_current (
            singleton_id INTEGER PRIMARY KEY,
            scope_id VARCHAR NOT NULL,
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reference_bundle_revision (
            content_hash VARCHAR PRIMARY KEY,
            bundle_json VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reference_bundle_current (
            singleton_id INTEGER PRIMARY KEY,
            content_hash VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resolution_policy_revision (
            policy_id VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            content_hash VARCHAR NOT NULL,
            policy_json VARCHAR NOT NULL,
            PRIMARY KEY (policy_id, version)
        );

        CREATE TABLE IF NOT EXISTS resolution_policy_current (
            singleton_id INTEGER PRIMARY KEY,
            policy_id VARCHAR NOT NULL,
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resolution_run (
            run_id VARCHAR PRIMARY KEY,
            project_id VARCHAR NOT NULL,
            staging_run_id VARCHAR NOT NULL,
            staging_content_hash VARCHAR NOT NULL,
            policy_hash VARCHAR NOT NULL,
            evaluation_hash VARCHAR NOT NULL,
            compared_pair_count BIGINT NOT NULL,
            scorer_version INTEGER NOT NULL,
            contract_version INTEGER NOT NULL,
            status VARCHAR NOT NULL,
            lifecycle_version BIGINT NOT NULL,
            published_at VARCHAR NOT NULL,
            published_by VARCHAR NOT NULL,
            effective_content_hash VARCHAR,
            decisions_hash VARCHAR,
            frozen_at VARCHAR,
            frozen_by VARCHAR,
            retired_at VARCHAR,
            retired_reason VARCHAR,
            successor_run_id VARCHAR
        );

        CREATE TABLE IF NOT EXISTS resolution_candidate (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            candidate_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            left_row_id VARCHAR NOT NULL,
            right_row_id VARCHAR NOT NULL,
            score VARCHAR NOT NULL,
            candidate_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal),
            UNIQUE (run_id, candidate_id)
        );

        CREATE INDEX IF NOT EXISTS resolution_candidate_lookup
            ON resolution_candidate (run_id, dataset, score, candidate_id);

        CREATE TABLE IF NOT EXISTS resolution_finding (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            finding_id VARCHAR NOT NULL,
            blocking BOOLEAN NOT NULL,
            finding_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal),
            UNIQUE (run_id, finding_id)
        );

        CREATE TABLE IF NOT EXISTS resolution_decision (
            run_id VARCHAR NOT NULL,
            lifecycle_version BIGINT NOT NULL,
            decision_id VARCHAR NOT NULL,
            group_id VARCHAR NOT NULL,
            field VARCHAR,
            kind VARCHAR NOT NULL,
            decision_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, lifecycle_version),
            UNIQUE (run_id, decision_id)
        );

        CREATE INDEX IF NOT EXISTS resolution_decision_lookup
            ON resolution_decision (run_id, group_id, field, kind);

        CREATE TABLE IF NOT EXISTS effective_row (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            row_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            effective_json VARCHAR NOT NULL,
            canonical_row_id VARCHAR,
            PRIMARY KEY (run_id, ordinal),
            UNIQUE (run_id, row_id)
        );

        CREATE INDEX IF NOT EXISTS effective_row_lookup
            ON effective_row (run_id, dataset, row_id);

        CREATE TABLE IF NOT EXISTS resolution_accounting (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            source_row_id VARCHAR NOT NULL,
            effective_row_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            accounting_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal),
            UNIQUE (run_id, source_row_id)
        );

        CREATE TABLE IF NOT EXISTS effective_dataset_reconciliation (
            run_id VARCHAR PRIMARY KEY,
            reconciliation_json VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resolution_current (
            singleton_id INTEGER PRIMARY KEY,
            run_id VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS effective_dataset_current (
            singleton_id INTEGER PRIMARY KEY,
            run_id VARCHAR NOT NULL
        );
        """
    )
