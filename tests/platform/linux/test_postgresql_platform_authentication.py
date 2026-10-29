"""Exercise platform login policy against disposable PostgreSQL on Linux."""

from __future__ import annotations

import hashlib
import os
import unittest
from datetime import UTC, datetime
from pathlib import PurePosixPath

import psycopg

from impodo.adapters.identity.scrypt_passwords import ScryptPasswordHasher
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.adapters.postgresql.platform_authentication import (
    PsycopgPlatformAuthenticationRepository,
)
from impodo.adapters.postgresql.platform_bootstrap import (
    PsycopgPlatformAdministratorBootstrapRepository,
)
from impodo.application.identity.authentication_service import (
    PlatformAuthenticationService,
)
from impodo.domain.identity.authentication import (
    PlatformAuthenticationCommand,
    PlatformAuthenticationRejectedError,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorStatus,
    SecurityEventKind,
)

NOW = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
BOOTSTRAP_EVENT_ID = "20000000-0000-4000-8000-000000000001"
PASSWORD = "fictional-platform-login-password"
SOURCE_DIGEST = "sha256:" + "b" * 64
OTHER_SOURCE_DIGEST = "sha256:" + "c" * 64
_DSN_REFERENCE = PurePosixPath("/run/secrets/postgres-runtime-dsn")


def _old_password_hash() -> PasswordHash:
    salt = b"o" * 16
    return PasswordHash(
        algorithm="scrypt",
        parameters=(("n", 16_384), ("r", 8), ("p", 1), ("dklen", 32)),
        salt=salt,
        digest=hashlib.scrypt(
            PASSWORD.encode(),
            salt=salt,
            n=16_384,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=32,
        ),
    )


class PostgreSQLPlatformAuthenticationLiveTests(unittest.TestCase):
    """Use fictional identities in the Linux-owned disposable database."""

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
            connection.execute(
                "TRUNCATE impodo_control.platform_administrator, "
                "impodo_control.authentication_throttle CASCADE"
            )
        PsycopgPlatformAdministratorBootstrapRepository(
            lambda _reference: self.dsn,
            _DSN_REFERENCE,
        ).bootstrap(
            PlatformAdministratorBootstrapRecord(
                administrator_id=ADMINISTRATOR_ID,
                normalized_login_email="operator@example.test",
                display_name="Fictional platform operator",
                status=PlatformAdministratorStatus.ACTIVE,
                security_revision=1,
                password_hash=_old_password_hash(),
                created_at=NOW,
            ),
            BootstrapSecurityEvent(
                event_id=BOOTSTRAP_EVENT_ID,
                deployment_id="local-trial",
                kind=SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
                subject_administrator_id=ADMINISTRATOR_ID,
                occurred_at=NOW,
            ),
        )

    def _service(self) -> PlatformAuthenticationService:
        identifiers = (
            f"30000000-0000-4000-8000-{index:012d}" for index in range(1, 100)
        )
        return PlatformAuthenticationService(
            PsycopgPlatformAuthenticationRepository(
                lambda _reference: self.dsn,
                _DSN_REFERENCE,
            ),
            ScryptPasswordHasher(salt_factory=lambda _size: b"n" * 16),
            clock=lambda: NOW,
            identifier_factory=lambda: next(identifiers),
        )

    def test_success_rehashes_atomically_and_records_bounded_events(self) -> None:
        result = self._service().authenticate(
            PlatformAuthenticationCommand(
                login_email="Operator@Example.Test",
                password=PASSWORD,
                source_digest=SOURCE_DIGEST,
            ),
            deployment_id="local-trial",
        )

        self.assertTrue(result.password_rehashed)
        with psycopg.connect(self.dsn) as connection:
            password_row = connection.execute(
                """
                SELECT parameters, password_revision
                  FROM impodo_control.platform_administrator_password_identity
                 WHERE administrator_id = %s
                """,
                (ADMINISTRATOR_ID,),
            ).fetchone()
            events = connection.execute(
                """
                SELECT event_kind, principal_hint_digest, source_digest
                  FROM impodo_control.platform_security_event
                 WHERE event_kind <> 'PLATFORM_ADMINISTRATOR_BOOTSTRAPPED'
                 ORDER BY event_kind
                """
            ).fetchall()

        self.assertIsNotNone(password_row)
        assert password_row is not None
        self.assertEqual(password_row[0]["n"], 32_768)
        self.assertEqual(password_row[1], 2)
        self.assertEqual(
            tuple(str(row[0]) for row in events),
            ("AUTHENTICATION_SUCCEEDED", "PASSWORD_REHASHED"),
        )
        self.assertTrue(all(str(row[1]).startswith("sha256:") for row in events))
        self.assertTrue(all(row[2] == SOURCE_DIGEST for row in events))

    def test_failed_attempts_lock_account_and_source_without_raw_values(self) -> None:
        service = self._service()
        command = PlatformAuthenticationCommand(
            login_email="operator@example.test",
            password="wrong-fictional-password",
            source_digest=SOURCE_DIGEST,
        )
        for _index in range(5):
            with self.assertRaises(PlatformAuthenticationRejectedError):
                service.authenticate(command, deployment_id="local-trial")

        with self.assertRaises(PlatformAuthenticationRejectedError):
            service.authenticate(
                PlatformAuthenticationCommand(
                    login_email="operator@example.test",
                    password=PASSWORD,
                    source_digest=OTHER_SOURCE_DIGEST,
                ),
                deployment_id="local-trial",
            )

        with psycopg.connect(self.dsn) as connection:
            throttles = connection.execute(
                """
                SELECT scope_kind, failure_count, locked_until IS NOT NULL
                  FROM impodo_control.authentication_throttle
                 ORDER BY scope_kind
                """
            ).fetchall()
            rejected_events = connection.execute(
                """
                SELECT count(*), bool_and(principal_hint_digest IS NOT NULL),
                       bool_and(source_digest IS NOT NULL)
                  FROM impodo_control.platform_security_event
                 WHERE event_kind = 'AUTHENTICATION_REJECTED'
                """
            ).fetchone()

        self.assertEqual(
            tuple((str(row[0]), int(row[1]), bool(row[2])) for row in throttles),
            (("ACCOUNT", 5, True), ("SOURCE", 5, True)),
        )
        self.assertEqual(tuple(rejected_events or ()), (6, True, True))


if __name__ == "__main__":
    unittest.main()
