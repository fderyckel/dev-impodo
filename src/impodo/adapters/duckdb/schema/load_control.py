"""Additive load settings and rejected-transport evidence for existing journals."""

import duckdb


def create_load_control_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create companions without rewriting historical snapshots or row JSON."""
    connection.execute("""
        CREATE TABLE execution_load_control (
            run_id VARCHAR PRIMARY KEY,
            control_json VARCHAR NOT NULL
        );
        CREATE TABLE execution_rejected_transport (
            run_id VARCHAR NOT NULL,
            transport_batch BIGINT NOT NULL,
            rejection_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, transport_batch)
        );
    """)
    create_load_control_event_schema(connection)


def create_load_control_event_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Upgrade both observed v4 shapes, preserving any existing event history.

    Early v4 stores predate the event companion. Later v4 stores already have
    it. Exact post-migration validation rejects an incompatible existing shape.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS execution_load_control_event (
            run_id VARCHAR NOT NULL,
            ordinal BIGINT NOT NULL,
            control_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, ordinal)
        );
    """)
