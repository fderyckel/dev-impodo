"""Create the worker-owned relationship-choice index for protected paging."""

import duckdb

RELATIONSHIP_CHOICE_COLUMNS = (
    "check_id",
    "ordinal",
    "owner_dataset_id",
    "target_field",
    "cause",
    "occurrence_count",
    "affected_row_count",
    "choice_json",
)


def create_relationship_choice_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Add an empty index; historical checks require explicit rechecking."""
    connection.execute("""
        CREATE TABLE matching_order_protected_relationship_choice (
            check_id VARCHAR NOT NULL, ordinal BIGINT NOT NULL,
            owner_dataset_id VARCHAR NOT NULL, target_field VARCHAR NOT NULL,
            cause VARCHAR NOT NULL, occurrence_count BIGINT NOT NULL,
            affected_row_count BIGINT NOT NULL, choice_json VARCHAR NOT NULL,
            PRIMARY KEY (check_id, ordinal)
        );
        CREATE INDEX matching_order_protected_relationship_choice_lookup
            ON matching_order_protected_relationship_choice
               (check_id, owner_dataset_id, target_field, cause, ordinal);
    """)
