"""Create the exact Project-owned DataVersion store generation."""

from __future__ import annotations

from pathlib import Path

import duckdb

from ....domain.data_version.models import DataVersion
from ....migration_foundation import MigrationStorageCompatibilityError
from .forward_upgrades import (
    SCHEMA_MIGRATION_COLUMNS,
    ForwardSchemaUpgrade,
    create_schema_migration_ledger,
    ensure_current_schema,
)


DATA_VERSION_STORE_GENERATION = "impodo-data-version-store-2026-08-project-owned"
DATA_VERSION_STORE_BASELINE_VERSION = 1
DATA_VERSION_STORE_VERSION = 2
EXPECTED_DATA_VERSION_STORE_COLUMNS = {
    "schema_version": ("singleton_id", "generation", "version"),
    "schema_migration": SCHEMA_MIGRATION_COLUMNS,
    "data_version_identity": (
        "singleton_id",
        "data_version_id",
        "project_id",
        "version_number",
        "state",
        "source_package_hash",
        "created_at",
    ),
    "source_package_state": (
        "singleton_id",
        "revision",
        "state",
        "origin",
        "package_hash",
        "updated_at",
        "frozen_at",
    ),
    "source_package_file": (
        "file_id",
        "display_name",
        "storage_key",
        "size_bytes",
        "sha256",
        "received_at",
    ),
    "source_package_catalog": (
        "file_id",
        "source_sha256",
        "catalog_hash",
        "catalog_json",
    ),
    "source_package_configuration": (
        "file_id",
        "catalog_hash",
        "configuration_hash",
        "configuration_json",
    ),
    "source_package_dataset": (
        "dataset_id",
        "display_name",
        "source_file_ids_json",
        "source_json",
        "row_count",
        "columns_json",
        "schema_hash",
        "snapshot_hash",
        "snapshot_storage_key",
        "manifest_json",
    ),
    "source_package_event": (
        "event_id",
        "revision",
        "event_type",
        "detail_json",
        "actor_issuer",
        "actor_subject",
        "actor_display_name",
        "occurred_at",
    ),
}


def initialize_data_version_store(
    connection: duckdb.DuckDBPyConnection,
    data_version: DataVersion,
) -> None:
    if _tables(connection):
        raise ValueError("DataVersion store is not empty")
    connection.execute(
        f"""
        CREATE TABLE schema_version (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            generation VARCHAR NOT NULL,
            version INTEGER NOT NULL
        );
        INSERT INTO schema_version VALUES (
            1, '{DATA_VERSION_STORE_GENERATION}', {DATA_VERSION_STORE_VERSION}
        );
        CREATE TABLE data_version_identity (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            data_version_id VARCHAR NOT NULL UNIQUE,
            project_id VARCHAR NOT NULL,
            version_number INTEGER NOT NULL,
            state VARCHAR NOT NULL,
            source_package_hash VARCHAR,
            created_at VARCHAR NOT NULL
        );
        CREATE TABLE source_package_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            revision INTEGER NOT NULL CHECK (revision >= 0),
            state VARCHAR NOT NULL CHECK (state IN ('DRAFT', 'FROZEN')),
            origin VARCHAR CHECK (origin IN ('FILE', 'ODOO')),
            package_hash VARCHAR,
            updated_at VARCHAR NOT NULL,
            frozen_at VARCHAR
        );
        CREATE TABLE source_package_file (
            file_id VARCHAR PRIMARY KEY,
            display_name VARCHAR NOT NULL,
            storage_key VARCHAR NOT NULL,
            size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
            sha256 VARCHAR NOT NULL,
            received_at VARCHAR NOT NULL
        );
        CREATE TABLE source_package_catalog (
            file_id VARCHAR PRIMARY KEY,
            source_sha256 VARCHAR NOT NULL,
            catalog_hash VARCHAR NOT NULL,
            catalog_json VARCHAR NOT NULL
        );
        CREATE TABLE source_package_configuration (
            file_id VARCHAR PRIMARY KEY,
            catalog_hash VARCHAR NOT NULL,
            configuration_hash VARCHAR NOT NULL,
            configuration_json VARCHAR NOT NULL
        );
        CREATE TABLE source_package_dataset (
            dataset_id VARCHAR PRIMARY KEY,
            display_name VARCHAR NOT NULL,
            source_file_ids_json VARCHAR NOT NULL,
            source_json VARCHAR NOT NULL,
            row_count BIGINT NOT NULL CHECK (row_count >= 0),
            columns_json VARCHAR NOT NULL,
            schema_hash VARCHAR NOT NULL,
            snapshot_hash VARCHAR NOT NULL,
            snapshot_storage_key VARCHAR NOT NULL,
            manifest_json VARCHAR NOT NULL
        );
        CREATE TABLE source_package_event (
            event_id VARCHAR PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            event_type VARCHAR NOT NULL,
            detail_json VARCHAR NOT NULL,
            actor_issuer VARCHAR NOT NULL,
            actor_subject VARCHAR NOT NULL,
            actor_display_name VARCHAR NOT NULL,
            occurred_at VARCHAR NOT NULL
        );
        """
    )
    create_schema_migration_ledger(connection)
    connection.execute(
        "INSERT INTO data_version_identity VALUES (1, ?, ?, ?, ?, ?, ?)",
        [
            data_version.data_version_id,
            data_version.project_id,
            data_version.version_number,
            data_version.state.value,
            data_version.source_package_hash,
            data_version.created_at.isoformat(),
        ],
    )
    connection.execute(
        "INSERT INTO source_package_state VALUES (1, 0, 'DRAFT', NULL, NULL, ?, NULL)",
        [data_version.created_at.isoformat()],
    )


def ensure_data_version_store(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
    data_version: DataVersion,
) -> None:
    try:
        ensure_current_schema(
            connection,
            expected_generation=DATA_VERSION_STORE_GENERATION,
            baseline_version=DATA_VERSION_STORE_BASELINE_VERSION,
            target_version=DATA_VERSION_STORE_VERSION,
            upgrades=DATA_VERSION_STORE_UPGRADES,
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
        SELECT data_version_id, project_id, version_number
          FROM data_version_identity
         WHERE singleton_id = 1
        """
    ).fetchone()
    if row != (
        data_version.data_version_id,
        data_version.project_id,
        data_version.version_number,
    ):
        raise _compatibility_error(database_path)


def _validate_current_schema(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
) -> None:
    if set(_tables(connection)) != set(EXPECTED_DATA_VERSION_STORE_COLUMNS):
        raise _compatibility_error(database_path)
    row = connection.execute(
        "SELECT generation, version FROM schema_version WHERE singleton_id = 1"
    ).fetchone()
    if row != (DATA_VERSION_STORE_GENERATION, DATA_VERSION_STORE_VERSION):
        raise _compatibility_error(database_path)
    for table, expected in EXPECTED_DATA_VERSION_STORE_COLUMNS.items():
        actual = tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        )
        if actual != expected:
            raise _compatibility_error(database_path)


def _upgrade_data_version_store_v1_to_v2(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    create_schema_migration_ledger(connection)


DATA_VERSION_STORE_UPGRADES = {
    1: ForwardSchemaUpgrade(
        migration_id="data-version-store-v1-to-v2-migration-ledger",
        apply=_upgrade_data_version_store_v1_to_v2,
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
