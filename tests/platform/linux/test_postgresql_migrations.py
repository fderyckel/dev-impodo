"""Exercise fresh and forward PostgreSQL migrations on disposable Linux CI."""

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

import psycopg
from psycopg import sql

from impodo.adapters.postgresql.control_schema import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
)
from impodo.adapters.postgresql.migrations import (
    POSTGRESQL_MIGRATIONS,
    PostgreSQLMigrationDefinition,
    PostgreSQLMigrationResult,
    PsycopgPostgreSQLMigrator,
)

_DSN_REFERENCE = PurePosixPath("/run/secrets/postgres-migration-dsn")
_CREATE_LEDGER_QUERY = """
CREATE TABLE impodo_control.schema_migration (
    version integer PRIMARY KEY CHECK (version > 0),
    name text NOT NULL CHECK (name <> ''),
    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at timestamp with time zone NOT NULL DEFAULT transaction_timestamp()
)
"""


class PostgreSQLMigrationLiveTests(unittest.TestCase):
    """Use only a disposable database named by the Linux-owned test lane."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("IMPODO_TEST_POSTGRES_DSN", "")
        if not cls.dsn:
            raise RuntimeError(
                "IMPODO_TEST_POSTGRES_DSN must name a disposable PostgreSQL database"
            )
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_roles WHERE rolname = 'impodo_runtime'
                    ) THEN
                        CREATE ROLE impodo_runtime NOLOGIN NOSUPERUSER NOCREATEDB
                            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                    END IF;
                    IF NOT EXISTS (
                        SELECT FROM pg_roles WHERE rolname = 'impodo_migrator'
                    ) THEN
                        CREATE ROLE impodo_migrator NOLOGIN NOSUPERUSER NOCREATEDB
                            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                    END IF;
                END
                $$
                """
            )
            database_name = connection.execute("SELECT current_database()").fetchone()
            if database_name is None:
                raise RuntimeError("disposable PostgreSQL database identity is missing")
            connection.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO impodo_migrator").format(
                    sql.Identifier(str(database_name[0]))
                )
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._drop_control_schema()

    def setUp(self) -> None:
        self._drop_control_schema()

    def tearDown(self) -> None:
        self._drop_control_schema()

    def test_two_fresh_migrators_serialize_and_replay_deterministically(self) -> None:
        def migrate(_index: int) -> PostgreSQLMigrationResult:
            return self._migrator().migrate(_DSN_REFERENCE)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(migrate, range(2)))

        self.assertEqual(
            sorted(len(result.applied_versions) for result in results),
            [0, len(POSTGRESQL_MIGRATIONS)],
        )
        self.assertTrue(
            all(
                result.current_version == POSTGRESQL_CONTROL_SCHEMA_VERSION
                for result in results
            )
        )
        self._assert_current_schema()

    def test_every_historical_prefix_upgrades_to_the_current_contract(self) -> None:
        for applied_count in range(1, len(POSTGRESQL_MIGRATIONS)):
            with self.subTest(applied_count=applied_count):
                self._drop_control_schema()
                self._seed_prefix(POSTGRESQL_MIGRATIONS[:applied_count])

                result = self._migrator().migrate(_DSN_REFERENCE)

                self.assertEqual(
                    result.applied_versions,
                    tuple(
                        migration.version
                        for migration in POSTGRESQL_MIGRATIONS[applied_count:]
                    ),
                )
                self.assertEqual(
                    self._migrator().migrate(_DSN_REFERENCE).applied_versions,
                    (),
                )
                self._assert_current_schema()

    @classmethod
    def _migrator(cls) -> PsycopgPostgreSQLMigrator:
        def read_dsn(reference: PurePosixPath) -> str:
            del reference
            return cls.dsn

        return PsycopgPostgreSQLMigrator(read_dsn)

    @classmethod
    def _seed_prefix(
        cls,
        migrations: tuple[PostgreSQLMigrationDefinition, ...],
    ) -> None:
        with psycopg.connect(cls.dsn) as connection:
            connection.execute("SET ROLE impodo_migrator")
            connection.execute("CREATE SCHEMA impodo_control")
            connection.execute(_CREATE_LEDGER_QUERY)
            for migration in migrations:
                for statement in migration.statements:
                    connection.execute(statement.query, statement.parameters)
                connection.execute(
                    """
                    INSERT INTO impodo_control.schema_migration
                        (version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, migration.checksum),
                )

    @classmethod
    def _assert_current_schema(cls) -> None:
        with psycopg.connect(cls.dsn) as connection:
            ledger = connection.execute(
                """
                SELECT version, name, checksum
                  FROM impodo_control.schema_migration
                 ORDER BY version
                """
            ).fetchall()
            contract = connection.execute(
                """
                SELECT generation, version
                  FROM impodo_control.schema_contract
                 WHERE contract_name = 'control'
                """
            ).fetchall()

        self_ledger = tuple(
            (migration.version, migration.name, migration.checksum)
            for migration in POSTGRESQL_MIGRATIONS
        )
        if tuple(tuple(row) for row in ledger) != self_ledger:
            raise AssertionError("PostgreSQL migration ledger is not current")
        if tuple(tuple(row) for row in contract) != (
            (
                POSTGRESQL_CONTROL_SCHEMA_GENERATION,
                POSTGRESQL_CONTROL_SCHEMA_VERSION,
            ),
        ):
            raise AssertionError("PostgreSQL schema contract is not current")

    @classmethod
    def _drop_control_schema(cls) -> None:
        if not getattr(cls, "dsn", ""):
            return
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")


if __name__ == "__main__":
    unittest.main()
