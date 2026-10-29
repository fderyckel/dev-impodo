"""Exercise concurrent first-operator bootstrap against disposable PostgreSQL."""

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import PurePosixPath

import psycopg

from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.adapters.postgresql.platform_bootstrap import (
    PsycopgPlatformAdministratorBootstrapRepository,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorAlreadyBootstrappedError,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorStatus,
    SecurityEventKind,
)

NOW = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
_DSN_REFERENCE = PurePosixPath("/run/secrets/postgres-runtime-dsn")


def _record(index: int) -> PlatformAdministratorBootstrapRecord:
    return PlatformAdministratorBootstrapRecord(
        administrator_id=f"10000000-0000-4000-8000-{index:012d}",
        normalized_login_email=f"operator-{index}@example.test",
        display_name=f"Fictional operator {index}",
        status=PlatformAdministratorStatus.ACTIVE,
        security_revision=1,
        password_hash=PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=index.to_bytes(16),
            digest=bytes([index]) * 32,
        ),
        created_at=NOW,
    )


def _event(index: int) -> BootstrapSecurityEvent:
    return BootstrapSecurityEvent(
        event_id=f"20000000-0000-4000-8000-{index:012d}",
        deployment_id="local-trial",
        kind=SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
        subject_administrator_id=_record(index).administrator_id,
        occurred_at=NOW,
    )


class PostgreSQLPlatformBootstrapLiveTests(unittest.TestCase):
    """Use only fictional rows in the Linux-owned disposable database."""

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
                END
                $$
                """
            )
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")
            connection.execute("CREATE SCHEMA impodo_control")
            for migration in POSTGRESQL_MIGRATIONS:
                for statement in migration.statements:
                    connection.execute(statement.query, statement.parameters)

    @classmethod
    def tearDownClass(cls) -> None:
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")

    def setUp(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute("TRUNCATE impodo_control.platform_administrator CASCADE")

    def _repository(self) -> PsycopgPlatformAdministratorBootstrapRepository:
        return PsycopgPlatformAdministratorBootstrapRepository(
            lambda _reference: self.dsn,
            _DSN_REFERENCE,
        )

    def test_two_simultaneous_bootstraps_commit_exactly_one_complete_bundle(
        self,
    ) -> None:
        def bootstrap(index: int) -> str:
            try:
                self._repository().bootstrap(_record(index), _event(index))
            except PlatformAdministratorAlreadyBootstrappedError:
                return "refused"
            return "committed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(bootstrap, (1, 2)))

        self.assertEqual(sorted(outcomes), ["committed", "refused"])
        with psycopg.connect(self.dsn) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM impodo_control.platform_administrator),
                    (SELECT count(*) FROM impodo_control.login_identity),
                    (SELECT count(*) FROM impodo_control.platform_administrator_password_identity),
                    (SELECT count(*) FROM impodo_control.platform_security_event)
                """
            ).fetchone()
            event_columns = connection.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'impodo_control'
                   AND table_name = 'platform_security_event'
                 ORDER BY ordinal_position
                """
            ).fetchall()

        self.assertEqual(tuple(counts or ()), (1, 1, 1, 1))
        self.assertEqual(
            tuple(str(row[0]) for row in event_columns),
            (
                "event_id",
                "deployment_id",
                "event_kind",
                "subject_administrator_id",
                "occurred_at",
                "principal_hint_digest",
                "source_digest",
            ),
        )


if __name__ == "__main__":
    unittest.main()
