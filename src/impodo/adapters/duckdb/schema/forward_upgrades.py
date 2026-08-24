"""Apply one-way DuckDB schema upgrades before normal repository access."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import duckdb


SCHEMA_MIGRATION_COLUMNS = (
    "from_version",
    "to_version",
    "migration_id",
)


@dataclass(frozen=True, slots=True)
class ForwardSchemaUpgrade:
    """One structural step between consecutive versions of one generation."""

    migration_id: str
    apply: Callable[[duckdb.DuckDBPyConnection], None]


def create_schema_migration_ledger(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Create deterministic evidence of the upgrades applied to this store."""

    connection.execute(
        """
        CREATE TABLE schema_migration (
            from_version INTEGER NOT NULL,
            to_version INTEGER PRIMARY KEY,
            migration_id VARCHAR NOT NULL UNIQUE,
            CHECK (to_version = from_version + 1)
        )
        """
    )


def ensure_current_schema(
    connection: duckdb.DuckDBPyConnection,
    *,
    expected_generation: str,
    baseline_version: int,
    target_version: int,
    upgrades: Mapping[int, ForwardSchemaUpgrade],
    validate_current: Callable[[], None],
    compatibility_error: Callable[[], Exception],
) -> None:
    """Upgrade a recognized older schema atomically, then validate it exactly.

    Upgrade keys are source versions. Each step must produce the next integer
    version. The complete path is resolved before any write, so an unsupported
    gap never partially changes a database.
    """

    try:
        row = connection.execute(
            """
            SELECT generation, version
              FROM schema_version
             WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None or str(row[0]) != expected_generation:
            raise compatibility_error()
        stored_version = int(row[1])
    except (duckdb.Error, TypeError, ValueError) as error:
        raise compatibility_error() from error

    if stored_version < baseline_version or stored_version > target_version:
        raise compatibility_error()
    if stored_version == target_version:
        validate_current()
        return

    path: list[tuple[int, ForwardSchemaUpgrade]] = []
    version = stored_version
    while version < target_version:
        upgrade = upgrades.get(version)
        if upgrade is None:
            raise compatibility_error()
        path.append((version, upgrade))
        version += 1

    try:
        connection.begin()
        for from_version, upgrade in path:
            upgrade.apply(connection)
            connection.execute(
                """
                INSERT INTO schema_migration (
                    from_version, to_version, migration_id
                ) VALUES (?, ?, ?)
                """,
                [from_version, from_version + 1, upgrade.migration_id],
            )
            connection.execute(
                """
                UPDATE schema_version
                   SET version = ?
                 WHERE singleton_id = 1
                """,
                [from_version + 1],
            )
        current_identity = connection.execute(
            """
            SELECT generation, version
              FROM schema_version
             WHERE singleton_id = 1
            """
        ).fetchone()
        if current_identity != (expected_generation, target_version):
            raise compatibility_error()
        validate_current()
        connection.commit()
    except Exception as error:
        try:
            connection.rollback()
        except duckdb.Error:
            pass
        raise compatibility_error() from error
