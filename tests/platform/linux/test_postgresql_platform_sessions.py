"""Exercise opaque server sessions against disposable PostgreSQL on Linux."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import psycopg

from impodo.adapters.identity.secure_sessions import SecureSessionSecretFactory
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.adapters.postgresql.platform_bootstrap import (
    PsycopgPlatformAdministratorBootstrapRepository,
)
from impodo.adapters.postgresql.platform_sessions import (
    PsycopgServerSessionRepository,
)
from impodo.application.identity.session_service import ServerSessionService
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorStatus,
    SecurityEventKind,
)
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    AccessServerSessionCommand,
    CreateServerSessionCommand,
    RevokeServerSessionCommand,
    RotateServerSessionCommand,
    ServerSessionRejectedError,
    SessionAuthenticationMethod,
    SessionSubject,
)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
_DSN_REFERENCE = PurePosixPath("/run/secrets/postgres-runtime-dsn")


class PostgreSQLPlatformSessionLiveTests(unittest.TestCase):
    """Use fictional sessions in the Linux-owned disposable database."""

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
                "TRUNCATE impodo_control.platform_administrator CASCADE"
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
                password_hash=PasswordHash(
                    algorithm="scrypt",
                    parameters=(
                        ("n", 32_768),
                        ("r", 8),
                        ("p", 1),
                        ("dklen", 32),
                    ),
                    salt=b"s" * 16,
                    digest=b"h" * 32,
                ),
                created_at=NOW,
            ),
            BootstrapSecurityEvent(
                event_id="20000000-0000-4000-8000-000000000002",
                deployment_id="local-trial",
                kind=SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
                subject_administrator_id=ADMINISTRATOR_ID,
                occurred_at=NOW,
            ),
        )

    def test_create_resolve_rotate_and_revoke_are_digest_only_and_atomic(
        self,
    ) -> None:
        observed_times = iter(
            NOW + timedelta(seconds=offset) for offset in range(1, 20)
        )
        event_ids = (
            f"30000000-0000-4000-8000-{index:012d}" for index in range(1, 20)
        )
        service = ServerSessionService(
            PsycopgServerSessionRepository(
                lambda _reference: self.dsn,
                _DSN_REFERENCE,
            ),
            SecureSessionSecretFactory(),
            clock=lambda: next(observed_times),
            identifier_factory=lambda: next(event_ids),
        )
        created = service.create(
            CreateServerSessionCommand(
                subject=SessionSubject(
                    principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
                    principal_id=ADMINISTRATOR_ID,
                    organization_id=None,
                ),
                authentication_method=SessionAuthenticationMethod.PASSWORD,
                security_revision=1,
            ),
            deployment_id="local-trial",
        )

        with self.assertRaises(ServerSessionRejectedError):
            service.access(
                AccessServerSessionCommand(
                    handle=created.handle,
                    csrf_token="invalid-csrf-token",
                    require_csrf=True,
                )
            )
        current = service.access(
            AccessServerSessionCommand(
                handle=created.handle,
                csrf_token=created.csrf_token,
                require_csrf=True,
            )
        )
        rotated = service.rotate_for_elevation(
            RotateServerSessionCommand(
                handle=created.handle,
                authentication_method=SessionAuthenticationMethod.WEBAUTHN,
                security_revision=current.security_revision,
            ),
            deployment_id="local-trial",
        )
        with self.assertRaises(ServerSessionRejectedError):
            service.access(AccessServerSessionCommand(handle=created.handle))
        current = service.access(
            AccessServerSessionCommand(
                handle=rotated.handle,
                csrf_token=rotated.csrf_token,
                require_csrf=True,
            )
        )
        service.revoke(
            RevokeServerSessionCommand(
                actor=SessionSubject(
                    principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
                    principal_id=ADMINISTRATOR_ID,
                    organization_id=None,
                ),
                session_id=current.session_id,
                expected_revision=current.optimistic_revision,
            ),
            deployment_id="local-trial",
        )

        with psycopg.connect(self.dsn) as connection:
            session_row = connection.execute(
                """
                SELECT state, revocation_reason,
                       octet_length(handle_digest), octet_length(csrf_digest)
                  FROM impodo_control.server_session
                 WHERE session_id = %s
                """,
                (current.session_id,),
            ).fetchone()
            event_kinds = connection.execute(
                """
                SELECT event_kind
                  FROM impodo_control.platform_security_event
                 WHERE event_kind LIKE 'SESSION_%'
                 ORDER BY occurred_at, event_id
                """
            ).fetchall()

        self.assertEqual(
            tuple(session_row or ()),
            ("REVOKED", "ADMINISTRATOR", 32, 32),
        )
        self.assertEqual(
            tuple(str(row[0]) for row in event_kinds),
            ("SESSION_CREATED", "SESSION_ROTATED", "SESSION_REVOKED"),
        )
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("SET ROLE impodo_runtime")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "DELETE FROM impodo_control.server_session WHERE session_id = %s",
                    (current.session_id,),
                )


if __name__ == "__main__":
    unittest.main()
