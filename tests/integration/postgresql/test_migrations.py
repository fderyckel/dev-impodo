"""Prove atomic, locked, forward-only PostgreSQL schema migration."""

from __future__ import annotations

import unittest
from pathlib import PurePosixPath
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


def _ledger_rows() -> tuple[tuple[object, ...], ...]:
    return tuple(
        (migration.version, migration.name, migration.checksum)
        for migration in POSTGRESQL_MIGRATIONS
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
        self.assertEqual(
            result.applied_versions,
            tuple(migration.version for migration in POSTGRESQL_MIGRATIONS),
        )
        self.assertEqual(result.current_version, POSTGRESQL_MIGRATIONS[-1].version)
        self.assertIn("pg_advisory_xact_lock", session.events[0][1])
        self.assertEqual(session.events[0][2], (1_229_803_599, 1_146_047_815))
        self.assertIn("CREATE SCHEMA", session.events[1][1])
        self.assertIn("schema_migration", session.events[2][1])
        ledger_inserts = tuple(
            event
            for event in session.events
            if event[0] == "execute"
            and "INSERT INTO impodo_control.schema_migration" in event[1]
        )
        self.assertEqual(len(ledger_inserts), len(POSTGRESQL_MIGRATIONS))
        ledger_insert = ledger_inserts[0]
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
        expected_grant_count = sum(
            statement.query.startswith("GRANT ")
            for migration in POSTGRESQL_MIGRATIONS
            for statement in migration.statements
        )
        self.assertEqual(len(grant_events), expected_grant_count)
        self.assertTrue(all("impodo_runtime" in event[1] for event in grant_events))
        for migration, ledger_event in zip(
            POSTGRESQL_MIGRATIONS,
            ledger_inserts,
            strict=True,
        ):
            for statement in migration.statements:
                if not statement.query.startswith("GRANT "):
                    continue
                grant_event = next(
                    event for event in grant_events if event[1] == statement.query
                )
                self.assertLess(
                    session.events.index(grant_event),
                    session.events.index(ledger_event),
                )
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)
        self.assertTrue(session.closed)
        self.assertEqual(session.events[-2][0], "commit")
        self.assertEqual(session.events[-1][0], "close")

    def test_current_schema_is_a_no_op_after_lock_and_history_validation(self) -> None:
        session = _Session(
            [
                _ledger_rows(),
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
        session = _Session(
            [
                _ledger_rows(),
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

        session = _Session(
            [
                _ledger_rows(),
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
        for migration in POSTGRESQL_MIGRATIONS:
            with self.subTest(migration=migration.name):
                self.assertEqual(
                    migration.checksum,
                    calculate_postgresql_migration_checksum(migration.statements),
                )

    def test_catalogue_schema_is_organization_scoped_and_bounded(self) -> None:
        migration = POSTGRESQL_MIGRATIONS[1]
        schema = "\n".join(statement.query for statement in migration.statements)

        self.assertIn("impodo_control.organization", schema)
        self.assertIn("impodo_control.project_catalogue", schema)
        self.assertIn("PRIMARY KEY (organization_id, project_id)", schema)
        self.assertIn(
            "REFERENCES impodo_control.organization (organization_id)",
            schema,
        )
        self.assertIn("CHECK (retention_days BETWEEN 1 AND 3650)", schema)
        self.assertIn("current_receipt_hash", schema)
        self.assertNotIn("GRANT SELECT, INSERT, UPDATE, DELETE", schema)
        self.assertNotIn("source_row", schema)
        self.assertNotIn("target_value", schema)
        self.assertNotIn("recipe_payload", schema)
        for statement in migration.statements:
            if statement.query.startswith("CREATE INDEX"):
                self.assertIn("(organization_id,", statement.query)

    def test_coordination_schema_is_tenant_scoped_fenced_and_append_only(
        self,
    ) -> None:
        migration = POSTGRESQL_MIGRATIONS[2]
        schema = "\n".join(statement.query for statement in migration.statements)

        for table in (
            "operation_request",
            "data_plane_receipt",
            "projection_receipt",
            "workspace_lease",
        ):
            self.assertIn(f"impodo_control.{table}", schema)
        self.assertIn("UNIQUE (organization_id, idempotency_key)", schema)
        self.assertIn(
            "PRIMARY KEY (organization_id, project_id, workspace_id)",
            schema,
        )
        self.assertIn("fencing_token bigint NOT NULL", schema)
        self.assertIn("jsonb_typeof(artifacts) = 'array'", schema)
        self.assertIn("data_plane_receipt_hash", schema)
        self.assertNotIn("GRANT SELECT, INSERT, UPDATE, DELETE", schema)
        self.assertNotIn("GRANT SELECT, INSERT, DELETE", schema)
        self.assertNotIn("source_row", schema)
        self.assertNotIn("target_value", schema)
        for statement in migration.statements:
            if statement.query.startswith("CREATE INDEX"):
                self.assertIn("(organization_id,", statement.query)

    def test_current_projection_pointer_rejects_stale_replacement(self) -> None:
        migration = POSTGRESQL_MIGRATIONS[3]
        schema = "\n".join(statement.query for statement in migration.statements)

        self.assertIn("impodo_control.projection_current", schema)
        self.assertIn(
            "PRIMARY KEY (organization_id, project_id, projection_kind)",
            schema,
        )
        self.assertIn("source_committed_at", schema)
        self.assertIn("DISTINCT ON", schema)
        self.assertIn("d.committed_at DESC, o.operation_id DESC", schema)
        self.assertNotIn("GRANT SELECT, INSERT, UPDATE, DELETE", schema)

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
