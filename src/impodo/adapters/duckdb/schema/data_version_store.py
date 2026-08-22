"""Create the exact Project-owned DataVersion store generation for M1."""

from __future__ import annotations

from pathlib import Path

import duckdb

from ....data_versions import DataVersion
from ....migration_foundation import MigrationStorageCompatibilityError


DATA_VERSION_STORE_GENERATION = "impodo-data-version-store-2026-08-m1"
DATA_VERSION_STORE_VERSION = 1
EXPECTED_DATA_VERSION_STORE_COLUMNS = {
    "schema_version": ("singleton_id", "generation", "version"),
    "data_version_identity": (
        "singleton_id",
        "data_version_id",
        "project_id",
        "version_number",
        "state",
        "source_package_hash",
        "created_at",
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
        """
    )
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


def ensure_data_version_store(
    connection: duckdb.DuckDBPyConnection,
    database_path: Path,
    data_version: DataVersion,
) -> None:
    try:
        matches = _matches_exact_schema(connection)
    except duckdb.Error as error:
        raise _compatibility_error(database_path) from error
    if not matches:
        raise _compatibility_error(database_path)
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


def _matches_exact_schema(connection: duckdb.DuckDBPyConnection) -> bool:
    if set(_tables(connection)) != set(EXPECTED_DATA_VERSION_STORE_COLUMNS):
        return False
    row = connection.execute(
        "SELECT generation, version FROM schema_version WHERE singleton_id = 1"
    ).fetchone()
    if row != (DATA_VERSION_STORE_GENERATION, DATA_VERSION_STORE_VERSION):
        return False
    return all(
        tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        )
        == expected
        for table, expected in EXPECTED_DATA_VERSION_STORE_COLUMNS.items()
    )


def _tables(connection: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    return tuple(str(row[0]) for row in connection.execute("SHOW TABLES").fetchall())


def _compatibility_error(database_path: Path) -> MigrationStorageCompatibilityError:
    root = database_path.resolve().parents[4]
    command = (
        ".\\.venv\\Scripts\\python.exe scripts\\reset-development-storage.py "
        f'--root "{root}"'
    )
    return MigrationStorageCompatibilityError(str(database_path.resolve()), command)
