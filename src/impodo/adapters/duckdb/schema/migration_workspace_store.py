"""Create the exact isolated MigrationWorkspace reference store."""

from __future__ import annotations

from pathlib import Path

import duckdb

from ....migration_foundation import MigrationStorageCompatibilityError
from ....migration_workspaces import MigrationWorkspace
from ....workspace_access import WorkspaceAccessContext
from .forward_upgrades import (
    SCHEMA_MIGRATION_COLUMNS,
    ForwardSchemaUpgrade,
    create_schema_migration_ledger,
    ensure_current_schema,
)


MIGRATION_WORKSPACE_GENERATION = "impodo-migration-workspace-2026-08-reference-only"
MIGRATION_WORKSPACE_BASELINE_VERSION = 1
MIGRATION_WORKSPACE_VERSION = 2
EXPECTED_WORKSPACE_STORE_COLUMNS = {
    "schema_version": ("singleton_id", "generation", "version"),
    "schema_migration": SCHEMA_MIGRATION_COLUMNS,
    "workspace_linkage": (
        "singleton_id",
        "workspace_id",
        "project_id",
        "data_version_id",
        "migration_run_id",
        "recipe_application_id",
        "created_at",
    ),
    "workspace_source_projection": (
        "singleton_id",
        "projection_id",
        "package_hash",
        "created_at",
        "created_by",
    ),
    "workspace_source_dataset": (
        "dataset_id",
        "snapshot_hash",
    ),
}


def initialize_migration_workspace_store(
    connection: duckdb.DuckDBPyConnection,
    workspace: MigrationWorkspace,
) -> None:
    if _tables(connection):
        raise ValueError("MigrationWorkspace store is not empty")
    connection.execute(
        f"""
        CREATE TABLE schema_version (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            generation VARCHAR NOT NULL,
            version INTEGER NOT NULL
        );
        INSERT INTO schema_version VALUES (
            1, '{MIGRATION_WORKSPACE_GENERATION}', {MIGRATION_WORKSPACE_VERSION}
        );
        CREATE TABLE workspace_linkage (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            workspace_id VARCHAR NOT NULL UNIQUE,
            project_id VARCHAR NOT NULL,
            data_version_id VARCHAR NOT NULL,
            migration_run_id VARCHAR NOT NULL,
            recipe_application_id VARCHAR,
            created_at VARCHAR NOT NULL
        );
        CREATE TABLE workspace_source_projection (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            projection_id VARCHAR NOT NULL UNIQUE,
            package_hash VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            created_by VARCHAR NOT NULL
        );
        CREATE TABLE workspace_source_dataset (
            dataset_id VARCHAR PRIMARY KEY,
            snapshot_hash VARCHAR NOT NULL
        );
        """
    )
    create_schema_migration_ledger(connection)
    connection.execute(
        "INSERT INTO workspace_linkage VALUES (1, ?, ?, ?, ?, ?, ?)",
        [
            workspace.workspace_id,
            workspace.project_id,
            workspace.data_version_id,
            workspace.migration_run_id,
            workspace.recipe_application_id,
            workspace.created_at.isoformat(),
        ],
    )


def ensure_migration_workspace_store(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
    workspace: MigrationWorkspace,
) -> None:
    ensure_workspace_linkage(
        connection,
        database_path,
        WorkspaceAccessContext(
            project_id=workspace.project_id,
            workspace_id=workspace.workspace_id,
            data_version_id=workspace.data_version_id,
            migration_run_id=workspace.migration_run_id,
            recipe_application_id=workspace.recipe_application_id,
        ),
    )


def ensure_workspace_linkage(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
    expected: WorkspaceAccessContext,
) -> None:
    """Reject old, mixed, or cross-wired workspace stores before evidence reads."""

    try:
        ensure_current_schema(
            connection,
            expected_generation=MIGRATION_WORKSPACE_GENERATION,
            baseline_version=MIGRATION_WORKSPACE_BASELINE_VERSION,
            target_version=MIGRATION_WORKSPACE_VERSION,
            upgrades=MIGRATION_WORKSPACE_UPGRADES,
            validate_current=lambda: _validate_current_schema(
                connection,
                database_path,
            ),
            compatibility_error=lambda: _compatibility_error(database_path),
        )
    except MigrationStorageCompatibilityError:
        raise
    except duckdb.Error as error:
        raise _compatibility_error(database_path) from error
    row = connection.execute(
        """
        SELECT workspace_id, project_id, data_version_id,
               migration_run_id, recipe_application_id
          FROM workspace_linkage
         WHERE singleton_id = 1
        """
    ).fetchone()
    if row != (
        expected.workspace_id,
        expected.project_id,
        expected.data_version_id,
        expected.migration_run_id,
        expected.recipe_application_id,
    ):
        raise _compatibility_error(database_path)


def _validate_current_schema(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
) -> None:
    if set(_tables(connection)) != set(EXPECTED_WORKSPACE_STORE_COLUMNS):
        raise _compatibility_error(database_path)
    row = connection.execute(
        "SELECT generation, version FROM schema_version WHERE singleton_id = 1"
    ).fetchone()
    if row != (MIGRATION_WORKSPACE_GENERATION, MIGRATION_WORKSPACE_VERSION):
        raise _compatibility_error(database_path)
    for table, expected in EXPECTED_WORKSPACE_STORE_COLUMNS.items():
        actual = tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        )
        if actual != expected:
            raise _compatibility_error(database_path)


def _upgrade_migration_workspace_v1_to_v2(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    create_schema_migration_ledger(connection)


MIGRATION_WORKSPACE_UPGRADES = {
    1: ForwardSchemaUpgrade(
        migration_id="migration-workspace-v1-to-v2-migration-ledger",
        apply=_upgrade_migration_workspace_v1_to_v2,
    ),
}


def _tables(connection: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    return tuple(str(row[0]) for row in connection.execute("SHOW TABLES").fetchall())


def _compatibility_error(database_path: Path) -> MigrationStorageCompatibilityError:
    root = database_path.resolve().parents[4]
    command = (
        ".\\.venv\\Scripts\\python.exe scripts\\reset-development-storage.py "
        f'--root "{root}"'
    )
    return MigrationStorageCompatibilityError(str(database_path.resolve()), command)
