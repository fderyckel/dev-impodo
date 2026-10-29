"""Verify the authenticated Platform Administrator browser journey."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from impodo.adapters.identity.secure_recovery_tokens import HmacIdentifierDigester
from impodo.domain.identity.administration import (
    OrganizationPage,
    OrganizationStatus,
    OrganizationView,
)
from impodo.domain.identity.authentication import PlatformAuthenticationResult
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    CurrentSessionView,
    ServerSessionCredentials,
    ServerSessionRejectedError,
    SessionAuthenticationMethod,
    SessionSubject,
)
from impodo.web.composition.server_administration import (
    ServerAdministrationDependencies,
    create_server_administration_router,
)
from impodo.web.composition.server_session_http import (
    SERVER_CSRF_COOKIE_NAME,
    ServerBrowserSecurityMiddleware,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
SESSION_ID = "20000000-0000-4000-8000-000000000002"
HANDLE = f"{SESSION_ID}.opaque-handle"
CSRF_TOKEN = "independent-csrf-token"


def _session(*, optimistic_revision: int = 1) -> CurrentSessionView:
    return CurrentSessionView(
        session_id=SESSION_ID,
        subject=SessionSubject(
            principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
            principal_id=ADMINISTRATOR_ID,
            organization_id=None,
        ),
        authentication_method=SessionAuthenticationMethod.PASSWORD,
        security_revision=3,
        created_at=NOW,
        last_seen_at=NOW,
        idle_expires_at=NOW + timedelta(minutes=30),
        absolute_expires_at=NOW + timedelta(hours=12),
        elevated_until=None,
        optimistic_revision=optimistic_revision,
    )


class _Authenticator:
    def __init__(self) -> None:
        self.commands = []
        self.called_on_event_loop = False

    def authenticate(self, command, *, deployment_id):
        self.commands.append((command, deployment_id))
        self.called_on_event_loop = _has_running_event_loop()
        return PlatformAuthenticationResult(
            administrator_id=ADMINISTRATOR_ID,
            security_revision=3,
            authenticated_at=NOW,
            password_rehashed=False,
        )


class _Sessions:
    def __init__(self) -> None:
        self.accesses = []
        self.revocations = []
        self.called_on_event_loop = False

    def create(self, command, *, deployment_id):
        self.create_command = (command, deployment_id)
        self.called_on_event_loop = _has_running_event_loop()
        return ServerSessionCredentials(
            handle=HANDLE,
            csrf_token=CSRF_TOKEN,
            session=_session(),
        )

    def access(self, command):
        self.accesses.append(command)
        self.called_on_event_loop = self.called_on_event_loop or _has_running_event_loop()
        if command.handle != HANDLE or (
            command.require_csrf and command.csrf_token != CSRF_TOKEN
        ):
            raise ServerSessionRejectedError("Session is not available")
        return _session(optimistic_revision=len(self.accesses))

    def revoke(self, command, *, deployment_id):
        self.revocations.append((command, deployment_id))
        self.called_on_event_loop = self.called_on_event_loop or _has_running_event_loop()


class _Organizations:
    def __init__(self) -> None:
        self.queries = []
        self.creations = []
        self.called_on_event_loop = False

    def list_organizations(self, query):
        self.queries.append(query)
        self.called_on_event_loop = _has_running_event_loop()
        return OrganizationPage(
            items=(
                OrganizationView(
                    organization_id="acme",
                    display_name="Acme",
                    status=OrganizationStatus.ACTIVE,
                    policy_revision=1,
                    optimistic_revision=1,
                ),
            ),
            next_after_display_name=None,
            next_after_organization_id=None,
        )

    def create_organization(self, command, *, deployment_id):
        self.creations.append((command, deployment_id))
        self.called_on_event_loop = self.called_on_event_loop or _has_running_event_loop()
        return OrganizationView(
            organization_id=command.organization_id,
            display_name=command.display_name,
            status=OrganizationStatus.ACTIVE,
            policy_revision=1,
            optimistic_revision=1,
        )


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class ServerAdministrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authenticator = _Authenticator()
        self.sessions = _Sessions()
        self.organizations = _Organizations()
        dependencies = ServerAdministrationDependencies(
            authenticator=self.authenticator,
            sessions=self.sessions,
            organizations=self.organizations,
            identifier_digester=HmacIdentifierDigester(
                b"fictional-deployment-key" + b"x" * 32
            ),
            templates=Jinja2Templates(
                directory=ROOT / "src" / "impodo" / "web" / "templates"
            ),
            deployment_id="local-trial",
            session_absolute_timeout=timedelta(hours=12),
        )
        app = FastAPI()
        app.include_router(create_server_administration_router(dependencies))
        app.add_middleware(
            ServerBrowserSecurityMiddleware,
            public_url="https://impodo.example.test",
        )
        self.client = TestClient(
            app,
            base_url="https://impodo.example.test",
            follow_redirects=False,
        )

    def tearDown(self) -> None:
        self.client.close()

    def test_login_sets_independent_cookies_and_renders_organization_page(self) -> None:
        response = self.client.post(
            "/login",
            data={
                "login_email": "operator@example.test",
                "password": "fictional-login-password",
            },
            headers={"Origin": "https://impodo.example.test"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/organizations")
        self.assertEqual(self.client.cookies[SERVER_CSRF_COOKIE_NAME], CSRF_TOKEN)
        page = self.client.get("/admin/organizations")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Acme", page.text)
        self.assertIn("receives no customer Project", page.text)
        self.assertFalse(self.authenticator.called_on_event_loop)
        self.assertFalse(self.sessions.called_on_event_loop)
        self.assertFalse(self.organizations.called_on_event_loop)

    def test_create_organization_requires_the_session_bound_csrf_token(self) -> None:
        self._login()

        created = self.client.post(
            "/admin/organizations",
            data={
                "csrf_token": CSRF_TOKEN,
                "organization_id": "beta",
                "display_name": "Beta",
            },
            headers={"Origin": "https://impodo.example.test"},
        )
        rejected = self.client.post(
            "/admin/organizations",
            data={
                "csrf_token": "wrong-token",
                "organization_id": "gamma",
                "display_name": "Gamma",
            },
            headers={"Origin": "https://impodo.example.test"},
        )

        self.assertEqual(created.status_code, 303)
        self.assertEqual(len(self.organizations.creations), 1)
        command, deployment_id = self.organizations.creations[0]
        self.assertEqual(command.organization_id, "beta")
        self.assertEqual(command.actor.principal_id, ADMINISTRATOR_ID)
        self.assertEqual(deployment_id, "local-trial")
        self.assertEqual(rejected.status_code, 303)
        self.assertEqual(rejected.headers["location"], "/login")
        self.assertEqual(len(self.organizations.creations), 1)

    def test_logout_revokes_the_exact_touched_session(self) -> None:
        self._login()

        response = self.client.post(
            "/logout",
            data={"csrf_token": CSRF_TOKEN},
            headers={"Origin": "https://impodo.example.test"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        command, deployment_id = self.sessions.revocations[0]
        self.assertEqual(command.session_id, SESSION_ID)
        self.assertEqual(command.actor_administrator_id, ADMINISTRATOR_ID)
        self.assertEqual(deployment_id, "local-trial")

    def test_invalid_keyset_cursor_is_a_bounded_bad_request(self) -> None:
        self._login()

        response = self.client.get(
            "/admin/organizations?after_display_name=Acme"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("page cursor is invalid", response.text)
        self.assertEqual(self.client.cookies[SERVER_CSRF_COOKIE_NAME], CSRF_TOKEN)

    def _login(self) -> None:
        response = self.client.post(
            "/login",
            data={
                "login_email": "operator@example.test",
                "password": "fictional-login-password",
            },
            headers={"Origin": "https://impodo.example.test"},
        )
        self.assertEqual(response.status_code, 303)


if __name__ == "__main__":
    unittest.main()
