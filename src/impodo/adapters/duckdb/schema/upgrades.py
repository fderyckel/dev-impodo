"""Forward-only upgrades for projects created from the current baseline."""

from __future__ import annotations

from collections.abc import Callable

import duckdb


ProjectSchemaUpgrade = Callable[[duckdb.DuckDBPyConnection], None]

def _upgrade_v1_to_v2(connection: duckdb.DuckDBPyConnection) -> None:
    """Classify every project from the file-only baseline as FILE origin."""

    connection.execute(
        """
        ALTER TABLE project
        ADD COLUMN source_mode VARCHAR DEFAULT 'FILE'
        """
    )
    connection.execute(
        "ALTER TABLE project ALTER COLUMN source_mode SET NOT NULL"
    )


def _upgrade_v2_to_v3(connection: duckdb.DuckDBPyConnection) -> None:
    """Add prepared-backed canonical projection and sparse issue facts."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS canonical_prepared_projection (
            run_id VARCHAR NOT NULL,
            dataset_id VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            ordinal_start BIGINT NOT NULL,
            row_count BIGINT NOT NULL,
            prepared_snapshot_hash VARCHAR NOT NULL,
            projection_json VARCHAR NOT NULL,
            PRIMARY KEY (run_id, dataset_id),
            UNIQUE (run_id, dataset),
            UNIQUE (run_id, ordinal_start)
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


# Map the stored version to the function that produces the next version.
PROJECT_SCHEMA_UPGRADES: dict[int, ProjectSchemaUpgrade] = {
    1: _upgrade_v1_to_v2,
    2: _upgrade_v2_to_v3,
}


def apply_project_schema_upgrades(
    connection: duckdb.DuckDBPyConnection,
    *,
    stored_version: int,
    target_version: int,
) -> None:
    """Apply every registered upgrade atomically in ascending order."""

    upgrade_path: list[tuple[int, ProjectSchemaUpgrade]] = []
    version = stored_version
    while version < target_version:
        upgrade = PROJECT_SCHEMA_UPGRADES.get(version)
        if upgrade is None:
            raise RuntimeError(
                "Project database schema cannot be upgraded by this Impodo "
                f"version (missing upgrade from version {version})"
            )
        upgrade_path.append((version, upgrade))
        version += 1

    if not upgrade_path:
        return

    connection.begin()
    try:
        for version, upgrade in upgrade_path:
            upgrade(connection)
            connection.execute(
                """
                UPDATE schema_version
                   SET version = ?
                 WHERE singleton_id = 1
                """,
                [version + 1],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
