"""Add immutable assessment sidecars without rewriting historical snapshots."""

import duckdb

BUSINESS_ASSESSMENT_TABLES = {
    "business_write_observation": ("content_hash", "snapshot_hash", "observation_json"),
    "business_intent_assessment": (
        "content_hash",
        "snapshot_hash",
        "intent_hash",
        "assessment_json",
    ),
    "business_intent_current": (
        "singleton_id",
        "snapshot_hash",
        "observation_hash",
        "assessment_hash",
    ),
    "business_intent_approval": ("run_id", "assessment_hash", "approval_json"),
}


def create_business_intent_assessment_schema(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Preserve only already-started journal authority during a forward upgrade."""
    connection.execute("""
        CREATE TABLE business_write_observation (
            content_hash VARCHAR PRIMARY KEY, snapshot_hash VARCHAR NOT NULL,
            observation_json VARCHAR NOT NULL
        );
        CREATE TABLE business_intent_assessment (
            content_hash VARCHAR PRIMARY KEY, snapshot_hash VARCHAR NOT NULL,
            intent_hash VARCHAR, assessment_json VARCHAR NOT NULL
        );
        CREATE TABLE business_intent_current (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            snapshot_hash VARCHAR NOT NULL, observation_hash VARCHAR NOT NULL,
            assessment_hash VARCHAR NOT NULL
        );
        CREATE TABLE business_intent_approval (
            run_id VARCHAR PRIMARY KEY, assessment_hash VARCHAR, approval_json VARCHAR
        );
        INSERT INTO business_intent_approval (run_id)
            SELECT run_id FROM execution_run;
    """)
