"""Qualify the complete Phase 4 browser journey against disposable PostgreSQL."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from impodo.adapters.identity.scrypt_passwords import ScryptPasswordHasher
from impodo.adapters.identity.secure_recovery_tokens import SecureRecoveryTokenFactory
from impodo.adapters.postgresql.platform_sessions import _LOCK_CUSTOMER_USER_QUERY
from impodo.domain.identity.recovery import RecoveryTokenPurpose

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from tests.support.server_identity_browser import (
    ORIGIN,
    PASSWORD,
    PLATFORM_EMAIL,
    ServerFormParser,
    build_server_identity_application,
)


class ServerIdentityBrowserLiveTests(unittest.TestCase):
    """Keep all fictional identities and destructive setup in an explicit test DB."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("IMPODO_TEST_POSTGRES_DSN", "")
        if not cls.dsn:
            raise RuntimeError(
                "IMPODO_TEST_POSTGRES_DSN must name a disposable test database"
            )
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            if not connection.info.dbname.endswith("_test"):
                raise RuntimeError(
                    "The browser suite requires a database ending in _test"
                )
            connection.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'impodo_runtime') THEN CREATE ROLE impodo_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$"
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
                "TRUNCATE impodo_control.organization, impodo_control.platform_administrator, impodo_control.authentication_throttle CASCADE"
            )
        self.metrics = []
        self.app = build_server_identity_application(self.dsn, self.metrics)
        self.clients = []
        self.platform = self.client()
        self.login(self.platform, PLATFORM_EMAIL)

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()

    def client(self) -> TestClient:
        client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        self.clients.append(client)
        return client

    def post(self, client, path, data):
        return client.post(path, data=data, headers={"Origin": ORIGIN})

    def login(self, client, email, password=PASSWORD, expected=303):
        response = self.post(
            client, "/login", {"login_email": email, "password": password}
        )
        self.assertEqual(response.status_code, expected)
        return response

    def organization_action(self, organization, action, extra=None, expected=303):
        page = self.platform.get(f"/admin/organizations/{organization}")
        self.assertEqual(page.status_code, 200)
        path = f"/admin/organizations/{organization}/actions"
        values = ServerFormParser(page.text).form(path, operation=action)
        values.update(extra or {})
        response = self.post(self.platform, path, values)
        self.assertEqual(response.status_code, expected)
        return response

    def provision(self, organization="acme"):
        page = self.platform.get("/admin/organizations")
        values = ServerFormParser(page.text).form("/admin/organizations")
        values.update(organization_id=organization, display_name=organization.title())
        self.assertEqual(
            self.post(self.platform, "/admin/organizations", values).status_code, 303
        )
        link = self.organization_action(
            organization,
            "assign-administrator",
            {
                "login_email": f"admin@{organization}.example.test",
                "display_name": f"{organization.title()} Administrator",
            },
            expected=200,
        )
        self.activate(ServerFormParser(link.text).token())
        administrator = self.client()
        self.login(administrator, f"admin@{organization}.example.test")
        return administrator

    def activate(self, token, expected=200):
        client = self.client()
        page = client.get("/activate")
        values = ServerFormParser(page.text).form("/activate")
        values.update(token=token, password=PASSWORD, password_confirmation=PASSWORD)
        response = self.post(client, "/activate", values)
        self.assertEqual(response.status_code, expected)
        return response

    def create_user(
        self, administrator, email="person@acme.example.test", name="Fictional person"
    ):
        values = ServerFormParser(administrator.get("/admin/users").text).form(
            "/admin/users"
        )
        values.update(login_email=email, display_name=name)
        response = self.post(administrator, "/admin/users", values)
        self.assertEqual(response.status_code, 200)
        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT user_id FROM impodo_control.customer_user WHERE normalized_login_email = %s",
                (email,),
            ).fetchone()
        self.assertIsNotNone(row)
        return str(row[0]), ServerFormParser(response.text).token()

    def user_action(self, administrator, user_id, action, expected=303):
        path = f"/admin/users/{user_id}/actions"
        page = administrator.get(f"/admin/users/{user_id}")
        values = ServerFormParser(page.text).form(path, operation=action)
        response = self.post(administrator, path, values)
        self.assertEqual(response.status_code, expected)
        return response

    def test_current_session_uses_sign_out_and_revokes_the_exact_session(self):
        administrator = self.provision()
        page = administrator.get("/admin/sessions")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sign out this browser", page.text)
        with psycopg.connect(self.dsn) as connection:
            session_id = str(connection.execute(
                "SELECT session_id FROM impodo_control.server_session "
                "WHERE organization_id = 'acme' AND state = 'ACTIVE'"
            ).fetchone()[0])
        self.assertNotIn(f'action="/admin/sessions/{session_id}/revoke"', page.text)
        form = ServerFormParser(page.text).form("/logout")
        self.assertEqual(self.post(administrator, "/logout", form).status_code, 303)
        self.assertEqual(administrator.get("/account").status_code, 303)
        with psycopg.connect(self.dsn) as connection:
            state = connection.execute(
                "SELECT state FROM impodo_control.server_session WHERE session_id = %s",
                (session_id,),
            ).fetchone()[0]
        self.assertEqual(state, "REVOKED")

    def test_unissued_public_tokens_never_hash_and_attempts_are_bounded(self):
        client = self.client()
        form = ServerFormParser(client.get("/reset-password").text).form("/reset-password")
        token = SecureRecoveryTokenFactory().create(RecoveryTokenPurpose.PASSWORD_RESET).token
        form.update(token=token, password=PASSWORD, password_confirmation=PASSWORD)
        self.metrics.clear()
        with patch.object(ScryptPasswordHasher, "hash", side_effect=AssertionError("An unissued token must not hash")):
            statuses = [self.post(client, "/reset-password", form).status_code for _ in range(35)]
        self.assertIn(400, statuses)
        self.assertEqual(statuses[-1], 429)
        self.assertTrue(all(status in {400, 429} for status in statuses))
        self.assertTrue(all(metric.operation == "account_recovery.inspect_token" for metric in self.metrics))

    def test_distinct_users_share_the_organization_read_lock_while_suspension_waits(self):
        administrator = self.provision()
        ids = []
        for index in range(2):
            user_id, token = self.create_user(administrator, email=f"lock-{index}@acme.example.test")
            self.activate(token)
            ids.append(user_id)
        with psycopg.connect(self.dsn) as first, psycopg.connect(self.dsn) as second:
            first.execute(_LOCK_CUSTOMER_USER_QUERY, (ids[0], "acme")).fetchone()
            second.execute(_LOCK_CUSTOMER_USER_QUERY + " NOWAIT", (ids[1], "acme")).fetchone()
            with psycopg.connect(self.dsn) as writer:
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    writer.execute("SELECT organization_id FROM impodo_control.organization WHERE organization_id = 'acme' FOR UPDATE NOWAIT")
                writer.rollback()
        self.organization_action("acme", "suspend")
        self.assertEqual(administrator.get("/admin/users").status_code, 303)

    def test_bootstrap_to_first_user_reset_suspend_reactivate_revoke_and_deactivate(
        self,
    ):
        administrator = self.provision()
        user_id, token = self.create_user(administrator)
        replacement = self.user_action(
            administrator, user_id, "activation", expected=200
        )
        self.activate(token, expected=400)
        token = ServerFormParser(replacement.text).token()
        self.activate(token)
        self.activate(token, expected=400)
        user = self.client()
        self.assertEqual(
            self.login(user, "person@acme.example.test").headers["location"], "/account"
        )
        self.assertEqual(user.get("/account").status_code, 200)
        self.assertEqual(user.get("/admin/users").status_code, 403)
        self.assertEqual(user.get("/admin/organizations").status_code, 403)
        self.assertEqual(user.get("/projects").status_code, 404)

        second_browser = self.client()
        self.login(second_browser, "person@acme.example.test")
        reset = self.user_action(administrator, user_id, "reset", expected=200)
        token = ServerFormParser(reset.text).token()
        recovery = self.client()
        page = recovery.get("/reset-password")
        values = ServerFormParser(page.text).form("/reset-password")
        values.update(
            token=token,
            password=PASSWORD + " changed",
            password_confirmation=PASSWORD + " changed",
        )
        self.assertEqual(
            self.post(recovery, "/reset-password", values).status_code, 200
        )
        self.assertEqual(
            self.post(recovery, "/reset-password", values).status_code, 400
        )
        self.assertEqual(user.get("/account").status_code, 303)
        self.assertEqual(second_browser.get("/account").status_code, 303)
        self.login(user, "person@acme.example.test", expected=401)
        self.login(user, "person@acme.example.test", PASSWORD + " changed")

        self.user_action(administrator, user_id, "suspend")
        self.assertEqual(user.get("/account").status_code, 303)
        self.login(
            user, "person@acme.example.test", PASSWORD + " changed", expected=401
        )
        self.user_action(administrator, user_id, "reactivate")
        self.login(user, "person@acme.example.test", PASSWORD + " changed")
        sessions = administrator.get(f"/admin/sessions?user_id={user_id}")
        parser = ServerFormParser(sessions.text)
        target, values = next(
            (path, data) for path, data in parser.forms if path.endswith("/revoke")
        )
        self.assertEqual(self.post(administrator, target, values).status_code, 303)
        self.assertEqual(user.get("/account").status_code, 303)
        history = administrator.get(f"/admin/history?user_id={user_id}")
        self.assertIn("Customer Password Reset", history.text)
        self.assertIn("Authentication Succeeded", history.text)
        self.assertIn("Session Created", history.text)
        self.assertNotIn(token, history.text)
        self.user_action(administrator, user_id, "deactivate")
        self.login(
            user, "person@acme.example.test", PASSWORD + " changed", expected=401
        )

        self.organization_action("acme", "rename", {"display_name": "Acme renamed"})
        self.organization_action("acme", "reauthenticate")
        self.assertEqual(administrator.get("/admin/users").status_code, 303)
        self.login(administrator, "admin@acme.example.test")
        self.organization_action("acme", "suspend")
        self.assertEqual(administrator.get("/admin/users").status_code, 303)
        self.login(administrator, "admin@acme.example.test", expected=401)
        self.organization_action("acme", "reactivate")
        self.login(administrator, "admin@acme.example.test")
        self.assertTrue(all(metric.connection_count == 1 for metric in self.metrics))
        self.assertTrue(
            all(metric.pool_wait_milliseconds == 0 for metric in self.metrics)
        )
        with psycopg.connect(self.dsn) as connection:
            rehashes = connection.execute(
                "SELECT count(*) FROM impodo_control.server_security_event WHERE event_kind = 'PASSWORD_REHASHED'"
            ).fetchone()[0]
        self.assertEqual(rehashes, 0)

    def test_cross_tenant_and_stale_mutations_do_not_disclose_or_change_targets(self):
        administrator = self.provision("acme")
        other = self.provision("beta")
        foreign_id, _ = self.create_user(other, email="person@beta.example.test")
        own_id, token = self.create_user(administrator)
        self.activate(token)
        missing = str(uuid4())
        self.assertEqual(
            administrator.get(f"/admin/users/{foreign_id}").status_code, 404
        )
        self.assertEqual(administrator.get(f"/admin/users/{missing}").status_code, 404)
        path = f"/admin/users/{own_id}/actions"
        values = ServerFormParser(
            administrator.get(f"/admin/users/{own_id}").text
        ).form(path, operation="suspend")
        no_confirmation = dict(values, confirmation="")
        self.assertEqual(
            self.post(administrator, path, no_confirmation).status_code, 400
        )
        no_csrf = dict(values, csrf_token="wrong")
        self.assertEqual(self.post(administrator, path, no_csrf).status_code, 303)
        self.login(administrator, "admin@acme.example.test")
        values = ServerFormParser(
            administrator.get(f"/admin/users/{own_id}").text
        ).form(path, operation="suspend")
        foreign_values = dict(values, confirmation=foreign_id, organization_id="beta")
        self.assertEqual(
            self.post(
                administrator, f"/admin/users/{foreign_id}/actions", foreign_values
            ).status_code,
            404,
        )
        self.assertEqual(self.post(administrator, path, values).status_code, 303)
        self.assertEqual(self.post(administrator, path, values).status_code, 409)
        self.assertEqual(self.platform.get(f"/admin/users/{own_id}").status_code, 403)
        self.assertEqual(
            administrator.get(
                "/admin/history?before_occurred_at=2026-01-01T00:00:00"
            ).status_code,
            400,
        )
        self.assertEqual(
            administrator.get(f"/admin/users?after_user_id={foreign_id}").status_code,
            400,
        )
        for route in ("/admin/users", f"/admin/users/{own_id}", "/admin/history", "/admin/sessions"):
            with self.subTest(route=route):
                self.metrics.clear()
                with collect_duckdb_request_timings() as timings:
                    self.assertEqual(administrator.get(route).status_code, 200)
                self.assertEqual(timings.connection_count, 0)
                self.assertEqual(timings.schema_check_count, 0)
                # One session transaction and one bounded service read.
                self.assertEqual(sum(metric.connection_count for metric in self.metrics), 2)

    def test_administrator_replacement_recovery_expiry_and_generic_public_requests(
        self,
    ):
        administrator = self.provision()
        user_id, token = self.create_user(administrator)
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                "UPDATE impodo_control.account_recovery_token SET expires_at = issued_at + interval '1 microsecond' WHERE principal_id = %s",
                (user_id,),
            )
        self.activate(token, expected=400)
        for email in (
            PLATFORM_EMAIL,
            "unknown@example.test",
            "not-an-email",
            "admin@acme.example.test",
        ):
            client = self.client()
            page = client.get("/forgot-password")
            values = ServerFormParser(page.text).form("/forgot-password")
            values["login_email"] = email
            response = self.post(client, "/forgot-password", values)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(email, response.text)
            if email == PLATFORM_EMAIL:
                expected = response.text
            else:
                self.assertEqual(response.text, expected)

        replacement = self.organization_action(
            "acme",
            "assign-administrator",
            {
                "login_email": "replacement@acme.example.test",
                "display_name": "Replacement administrator",
            },
            expected=200,
        )
        self.assertEqual(administrator.get("/admin/users").status_code, 303)
        self.activate(ServerFormParser(replacement.text).token())
        self.login(administrator, "replacement@acme.example.test")
        reset = self.organization_action("acme", "administrator-reset", expected=200)
        client = self.client()
        values = ServerFormParser(client.get("/reset-password").text).form(
            "/reset-password"
        )
        values.update(
            token=ServerFormParser(reset.text).token(),
            password=PASSWORD,
            password_confirmation=PASSWORD,
        )
        self.assertEqual(self.post(client, "/reset-password", values).status_code, 200)
        self.assertEqual(administrator.get("/admin/users").status_code, 303)
        page = self.platform.get("/account")
        values = ServerFormParser(page.text).form("/account/platform-reset-link")
        result = self.post(self.platform, "/account/platform-reset-link", values)
        self.assertEqual(result.status_code, 200)
        values = ServerFormParser(client.get("/reset-password").text).form(
            "/reset-password"
        )
        values.update(
            token=ServerFormParser(result.text).token(),
            password=PASSWORD,
            password_confirmation=PASSWORD,
        )
        self.assertEqual(self.post(client, "/reset-password", values).status_code, 200)
        self.assertEqual(self.platform.get("/account").status_code, 303)
        self.login(self.platform, PLATFORM_EMAIL)
