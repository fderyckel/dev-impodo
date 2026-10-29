"""Exercise tenant-scoped identity administration against disposable PostgreSQL."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import uuid4

import psycopg

from impodo.adapters.identity.secure_recovery_tokens import SecureRecoveryTokenFactory
from impodo.adapters.postgresql.identity_administration import (
    PsycopgIdentityAdministrationRepository,
)
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.adapters.postgresql.platform_bootstrap import (
    PsycopgPlatformAdministratorBootstrapRepository,
)
from impodo.application.identity.administration_service import (
    IdentityAdministrationService,
)
from impodo.domain.identity.administration import (
    ActivateCustomerUserCommand,
    AdministrationActor,
    AdministrationEventQuery,
    CreateCustomerUserCommand,
    CreateOrganizationCommand,
    CustomerUserMutationCommand,
    CustomerUserStatus,
    OrganizationSessionQuery,
    ReplaceOrganizationAdministratorCommand,
    RevokeOrganizationSessionCommand,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorStatus,
    SecurityEventKind,
)
from impodo.domain.identity.recovery import LoginPrincipalKind, PasswordResetCommand

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
PLATFORM_ID = "10000000-0000-4000-8000-000000000001"
_DSN_REFERENCE = PurePosixPath("/run/secrets/postgres-runtime-dsn")


class _PasswordHasher:
    def hash(self, password: str) -> PasswordHash:
        del password
        return PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=b"s" * 16,
            digest=b"h" * 32,
        )

    def verify(self, password: str, password_hash: PasswordHash) -> bool:
        del password, password_hash
        return False

    def needs_rehash(self, password_hash: PasswordHash) -> bool:
        del password_hash
        return False


class PostgreSQLIdentityAdministrationLiveTests(unittest.TestCase):
    """Use only fictional identities in the Linux-owned disposable database."""

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
                administrator_id=PLATFORM_ID,
                normalized_login_email="operator@example.test",
                display_name="Fictional platform operator",
                status=PlatformAdministratorStatus.ACTIVE,
                security_revision=1,
                password_hash=_PasswordHasher().hash("not persisted"),
                created_at=NOW,
            ),
            BootstrapSecurityEvent(
                event_id="20000000-0000-4000-8000-000000000002",
                deployment_id="local-trial",
                kind=SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
                subject_administrator_id=PLATFORM_ID,
                occurred_at=NOW,
            ),
        )

    def test_full_tenant_lifecycle_is_atomic_scoped_and_digest_only(self) -> None:
        observed = iter(NOW + timedelta(seconds=value) for value in range(1, 100))
        service = IdentityAdministrationService(
            PsycopgIdentityAdministrationRepository(
                lambda _reference: self.dsn,
                _DSN_REFERENCE,
            ),
            SecureRecoveryTokenFactory(),
            _PasswordHasher(),
            clock=lambda: next(observed),
            identifier_factory=lambda: str(uuid4()),
        )
        platform = AdministrationActor(
            principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
            principal_id=PLATFORM_ID,
            organization_id=None,
        )
        organization = service.create_organization(
            CreateOrganizationCommand(
                actor=platform,
                organization_id="acme",
                display_name="Acme",
            ),
            deployment_id="local-trial",
        )
        provisioned_admin = service.replace_organization_administrator(
            ReplaceOrganizationAdministratorCommand(
                actor=platform,
                organization_id="acme",
                login_email="admin@example.test",
                display_name="Acme Administrator",
                expected_organization_revision=organization.optimistic_revision,
            ),
            deployment_id="local-trial",
        )
        active_admin = service.activate_customer_user(
            ActivateCustomerUserCommand(
                token=provisioned_admin.activation_token,
                password="fictional administrator password",
                source_digest="source-not-persisted",
            ),
            deployment_id="local-trial",
        )
        actor = AdministrationActor(
            principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
            principal_id=active_admin.user_id,
            organization_id="acme",
        )
        provisioned_user = service.create_customer_user(
            CreateCustomerUserCommand(
                actor=actor,
                organization_id="acme",
                login_email="person@example.test",
                display_name="Fictional Person",
            ),
            deployment_id="local-trial",
        )
        active_user = service.activate_customer_user(
            ActivateCustomerUserCommand(
                token=provisioned_user.activation_token,
                password="fictional customer password",
                source_digest="source-not-persisted",
            ),
            deployment_id="local-trial",
        )

        session_id = str(uuid4())
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                """
                INSERT INTO impodo_control.server_session (
                    session_id, principal_kind, principal_id, organization_id,
                    handle_digest, csrf_digest, authentication_method,
                    security_revision, state, created_at, last_seen_at,
                    idle_expires_at, absolute_expires_at, optimistic_revision
                ) VALUES (
                    %s, 'CUSTOMER_USER', %s, 'acme', %s, %s, 'PASSWORD',
                    %s, 'ACTIVE', %s, %s, %s, %s, 1
                )
                """,
                (
                    session_id,
                    active_user.user_id,
                    b"h" * 32,
                    b"c" * 32,
                    active_user.security_revision,
                    NOW,
                    NOW,
                    NOW + timedelta(minutes=30),
                    NOW + timedelta(hours=8),
                ),
            )
        sessions = service.list_sessions(
            OrganizationSessionQuery(actor=actor, organization_id="acme")
        )
        self.assertEqual(tuple(item.session_id for item in sessions.items), (session_id,))
        service.revoke_session(
            RevokeOrganizationSessionCommand(
                actor=actor,
                organization_id="acme",
                session_id=session_id,
                expected_revision=1,
            ),
            deployment_id="local-trial",
        )

        reset = service.issue_customer_reset(
            CustomerUserMutationCommand(
                actor=actor,
                organization_id="acme",
                user_id=active_user.user_id,
                expected_revision=active_user.optimistic_revision,
            ),
            deployment_id="local-trial",
        )
        reset_user = service.reset_customer_password(
            PasswordResetCommand(
                token=reset.token,
                new_password="fictional replacement password",
                source_digest="source-not-persisted",
            ),
            deployment_id="local-trial",
        )
        suspended_user = service.suspend_customer_user(
            CustomerUserMutationCommand(
                actor=actor,
                organization_id="acme",
                user_id=reset_user.user_id,
                expected_revision=reset_user.optimistic_revision,
            ),
            deployment_id="local-trial",
        )
        events = service.list_security_events(
            AdministrationEventQuery(actor=actor, organization_id="acme")
        )
        user_events = service.list_security_events(
            AdministrationEventQuery(
                actor=actor,
                organization_id="acme",
                user_id=active_user.user_id,
            )
        )

        self.assertEqual(suspended_user.status, CustomerUserStatus.SUSPENDED)
        self.assertGreaterEqual(len(events.items), 8)
        self.assertTrue(user_events.items)
        self.assertTrue(
            all(item.subject_user_id == active_user.user_id for item in user_events.items)
        )
        self.assertNotIn(provisioned_user.activation_token, repr(events))
        self.assertNotIn(reset.token, repr(events))
        with psycopg.connect(self.dsn) as connection:
            stored = connection.execute(
                """
                SELECT s.state, s.revocation_reason,
                       count(t.token_digest), min(o.optimistic_revision)
                  FROM impodo_control.server_session AS s
                  CROSS JOIN impodo_control.organization AS o
                  LEFT JOIN impodo_control.account_recovery_token AS t
                    ON t.principal_id = s.principal_id
                 WHERE s.session_id = %s AND o.organization_id = 'acme'
                 GROUP BY s.state, s.revocation_reason
                """,
                (session_id,),
            ).fetchone()
        self.assertEqual(tuple(stored or ())[:2], ("REVOKED", "ADMINISTRATOR"))
        self.assertGreaterEqual(int(tuple(stored or (0, 0, 0))[2]), 2)

        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("SET ROLE impodo_runtime")
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                connection.execute(
                    """
                    UPDATE impodo_control.organization_administrator_assignment
                       SET user_id = %s,
                           assignment_revision = assignment_revision + 1
                     WHERE organization_id = 'acme'
                    """,
                    (active_user.user_id,),
                )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "DELETE FROM impodo_control.customer_user WHERE user_id = %s",
                    (active_user.user_id,),
                )


if __name__ == "__main__":
    unittest.main()
