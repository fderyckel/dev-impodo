"""Prove atomic, locked, forward-only PostgreSQL schema migration."""

from __future__ import annotations

from pathlib import PurePosixPath
import unittest
from typing import LiteralString
from unittest.mock import Mock, patch

from impodo.adapters.postgresql.control_schema import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
    POSTGRESQL_MIGRATION_ROLE,
)
from impodo.adapters.postgresql.migrations import (
    POSTGRESQL_MIGRATIONS,
    PostgreSQLMigrationExecutionError,
    PostgreSQLMigrationIncompatibleError,
    PostgreSQLMigrationUnavailableError,
    PsycopgPostgreSQLMigrator,
    calculate_postgresql_migration_checksum,
    open_psycopg_migration_session,
)
from impodo.adapters.postgresql.startup import PostgreSQLDsnError


class _Session:
    def __init__(
        self,
        responses: list[tuple[tuple[object, ...], ...]],
        *,
        execute_error_at: int | None = None,
        commit_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.execute_error_at = execute_error_at
        self.commit_error = commit_error
        self.close_error = close_error
        self.events: list[tuple[str, str, tuple[object, ...]]] = []
        self.execute_count = 0
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None:
        self.events.append(("execute", query, parameters))
        self.execute_count += 1
        if self.execute_error_at == self.execute_count:
            raise PostgreSQLMigrationExecutionError("private database detail")

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        self.events.append(("fetch", query, parameters))
        return self.responses.pop(0)

    def commit(self) -> None:
        self.events.append(("commit", "", ()))
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    def rollback(self) -> None:
        self.events.append(("rollback", "", ()))
        self.rolled_back = True

    def close(self) -> None:
        self.events.append(("close", "", ()))
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _contract_rows() -> tuple[tuple[object, ...], ...]:
    return (
        (
            POSTGRESQL_CONTROL_SCHEMA_GENERATION,
            POSTGRESQL_CONTROL_SCHEMA_VERSION,
        ),
    )


class PostgreSQLMigrationTests(unittest.TestCase):
    def test_first_run_locks_bootstraps_applies_ledgers_and_commits_once(self) -> None:
        session = _Session([(), _contract_rows()])
        opened_dsns: list[str] = []

        def open_session(dsn: str) -> _Session:
            opened_dsns.append(dsn)
            return session

        migrator = PsycopgPostgreSQLMigrator(
            lambda _reference: "postgresql://migrator:secret@database/impodo",
            session_factory=open_session,
        )

        result = migrator.migrate(PurePosixPath("/run/secrets/postgres-migration-dsn"))

        self.assertEqual(len(opened_dsns), 1)
        self.assertEqual(result.applied_versions, (1,))
        self.assertEqual(result.current_version, 1)
        self.assertIn("pg_advisory_xact_lock", session.events[0][1])
        self.assertEqual(session.events[0][2], (1_229_803_599, 1_146_047_815))
        self.assertIn("CREATE SCHEMA", session.events[1][1])
        self.assertIn("schema_migration", session.events[2][1])
        ledger_insert = next(
            event
            for event in session.events
            if event[0] == "execute"
            and "INSERT INTO impodo_control.schema_migration" in event[1]
        )
        self.assertEqual(
            ledger_insert[2],
            (
                1,
                POSTGRESQL_MIGRATIONS[0].name,
                POSTGRESQL_MIGRATIONS[0].checksum,
            ),
        )
        grant_events = tuple(
            event
            for event in session.events
            if event[0] == "execute" and event[1].startswith("GRANT ")
        )
        self.assertEqual(len(grant_events), 2)
        self.assertTrue(all("impodo_runtime" in event[1] for event in grant_events))
        self.assertLess(
            session.events.index(grant_events[-1]), session.events.index(ledger_insert)
        )
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)
        self.assertTrue(session.closed)
        self.assertEqual(session.events[-2][0], "commit")
        self.assertEqual(session.events[-1][0], "close")

    def test_current_schema_is_a_no_op_after_lock_and_history_validation(self) -> None:
        migration = POSTGRESQL_MIGRATIONS[0]
        session = _Session(
            [
                ((migration.version, migration.name, migration.checksum),),
                _contract_rows(),
            ]
        )
        migrator = PsycopgPostgreSQLMigrator(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )

        result = migrator.migrate(PurePosixPath("/run/secrets/migration-dsn"))

        self.assertEqual(result.applied_versions, ())
        self.assertFalse(
            any(
                "CREATE TABLE impodo_control.schema_contract" in event[1]
                for event in session.events
            )
        )
        self.assertTrue(session.committed)

    def test_unknown_or_changed_history_rolls_back_without_applying(self) -> None:
        migration = POSTGRESQL_MIGRATIONS[0]
        cases = (
            ((2, "future", "0" * 64),),
            ((1, migration.name, "0" * 64),),
            ((1, "renamed", migration.checksum),),
        )
        for rows in cases:
            with self.subTest(rows=rows):
                session = _Session([rows])
                migrator = PsycopgPostgreSQLMigrator(
                    lambda _reference: "private-dsn",
                    session_factory=lambda _dsn, session=session: session,
                )

                with self.assertRaises(PostgreSQLMigrationIncompatibleError):
                    migrator.migrate(PurePosixPath("/run/secrets/migration-dsn"))

                self.assertTrue(session.rolled_back)
                self.assertFalse(session.committed)
                self.assertTrue(session.closed)

    def test_current_history_with_wrong_contract_rolls_back(self) -> None:
        migration = POSTGRESQL_MIGRATIONS[0]
        session = _Session(
            [
                ((migration.version, migration.name, migration.checksum),),
                (("future-generation", 99),),
            ]
        )
        migrator = PsycopgPostgreSQLMigrator(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )

        with self.assertRaises(PostgreSQLMigrationIncompatibleError) as caught:
            migrator.migrate(PurePosixPath("/run/secrets/migration-dsn"))

        self.assertNotIn("future-generation", str(caught.exception))
        self.assertTrue(session.rolled_back)
        self.assertFalse(session.committed)

    def test_statement_failure_rolls_back_without_exposing_database_detail(
        self,
    ) -> None:
        session = _Session([()], execute_error_at=4)
        migrator = PsycopgPostgreSQLMigrator(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )

        with self.assertRaises(PostgreSQLMigrationExecutionError) as caught:
            migrator.migrate(PurePosixPath("/run/secrets/migration-dsn"))

        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(session.rolled_back)
        self.assertFalse(session.committed)
        self.assertTrue(session.closed)

    def test_dsn_and_close_failures_are_bounded(self) -> None:
        def fail_dsn(_reference: PurePosixPath) -> str:
            raise PostgreSQLDsnError("postgresql://migrator:secret@database")

        migrator = PsycopgPostgreSQLMigrator(fail_dsn)
        with self.assertRaises(PostgreSQLMigrationUnavailableError) as caught:
            migrator.migrate(PurePosixPath("/run/secrets/migration-dsn"))
        self.assertNotIn("secret", str(caught.exception))

        migration = POSTGRESQL_MIGRATIONS[0]
        session = _Session(
            [
                ((migration.version, migration.name, migration.checksum),),
                _contract_rows(),
            ],
            close_error=PostgreSQLMigrationUnavailableError("database.internal"),
        )
        migrator = PsycopgPostgreSQLMigrator(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )
        with self.assertRaises(PostgreSQLMigrationUnavailableError) as caught:
            migrator.migrate(PurePosixPath("/run/secrets/migration-dsn"))
        self.assertNotIn("internal", str(caught.exception))
        self.assertTrue(session.committed)

    def test_checked_in_checksum_matches_the_immutable_migration(self) -> None:
        migration = POSTGRESQL_MIGRATIONS[0]

        self.assertEqual(
            migration.checksum,
            calculate_postgresql_migration_checksum(migration.statements),
        )

    def test_default_psycopg_session_is_bounded_and_transactional(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.migrations.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_migration_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["conninfo"], "private-dsn")
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertEqual(kwargs["application_name"], "impodo-migrate")
        self.assertIn(f"role={POSTGRESQL_MIGRATION_ROLE}", kwargs["options"])
        self.assertIn("statement_timeout=60000", kwargs["options"])
        self.assertIn("lock_timeout=5000", kwargs["options"])
        self.assertIn("idle_in_transaction_session_timeout=60000", kwargs["options"])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
