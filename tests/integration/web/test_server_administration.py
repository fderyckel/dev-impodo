"""Verify the authenticated Platform Administrator browser journey."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from impodo.adapters.identity.secure_recovery_tokens import HmacIdentifierDigester
from impodo.domain.identity.administration import (
    CustomerUserPage,
    CustomerUserStatus,
    CustomerUserView,
    AdministrationEventPage,
    OrganizationAdministrationView,
    OrganizationSessionPage,
    ProvisionedCustomerUserResult,
    OrganizationPage,
    OrganizationStatus,
    OrganizationView,
)
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.server_authentication import ServerAuthenticationResult
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
from impodo.web.composition.local_app import create_local_app
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from tests.support.server_identity_browser import ServerFormParser
from impodo.web.composition.server_session_http import (
    SERVER_CSRF_COOKIE_NAME,
    ServerBrowserSecurityMiddleware,
)
from impodo.web.composition.server_account_admission import ServerAccountAdmission

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
SESSION_ID = "20000000-0000-4000-8000-000000000002"
CUSTOMER_ID = "30000000-0000-4000-8000-000000000003"
HANDLE = f"{SESSION_ID}.opaque-handle"
CSRF_TOKEN = "independent-csrf-token"


def _session(
    *,
    optimistic_revision: int = 1,
    principal_kind: LoginPrincipalKind = LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
    organization_id: str | None = None,
) -> CurrentSessionView:
    return CurrentSessionView(
        session_id=SESSION_ID,
        subject=SessionSubject(
            principal_kind=principal_kind,
            principal_id=ADMINISTRATOR_ID,
            organization_id=organization_id,
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
        self.principal_kind = LoginPrincipalKind.PLATFORM_ADMINISTRATOR
        self.organization_id = None

    def authenticate(self, command, *, deployment_id):
        self.commands.append((command, deployment_id))
        self.called_on_event_loop = _has_running_event_loop()
        return ServerAuthenticationResult(
            principal_kind=self.principal_kind,
            principal_id=ADMINISTRATOR_ID,
            organization_id=self.organization_id,
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
            session=_session(
                principal_kind=command.subject.principal_kind,
                organization_id=command.subject.organization_id,
            ),
        )

    def access(self, command):
        self.accesses.append(command)
        self.called_on_event_loop = self.called_on_event_loop or _has_running_event_loop()
        if command.handle != HANDLE or (
            command.require_csrf and command.csrf_token != CSRF_TOKEN
        ):
            raise ServerSessionRejectedError("Session is not available")
        command, _deployment_id = self.create_command
        return _session(
            optimistic_revision=len(self.accesses),
            principal_kind=command.subject.principal_kind,
            organization_id=command.subject.organization_id,
        )

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

    def list_customer_users(self, query):
        self.user_queries = getattr(self, "user_queries", [])
        self.user_queries.append(query)
        return CustomerUserPage(
            items=(
                CustomerUserView(
                    user_id=CUSTOMER_ID,
                    organization_id=query.organization_id,
                    normalized_login_email="user@example.test",
                    display_name="Fictional user",
                    principal_kind=LoginPrincipalKind.CUSTOMER_USER,
                    status=CustomerUserStatus.ACTIVE,
                    security_revision=1,
                    optimistic_revision=1,
                ),
            ),
            next_after_user_id=None,
        )

    def get_customer_user(self, query):
        return CustomerUserView(
            user_id=query.user_id,
            organization_id=query.organization_id,
            normalized_login_email="user@example.test",
            display_name="Fictional user",
            principal_kind=LoginPrincipalKind.CUSTOMER_USER,
            status=CustomerUserStatus.ACTIVE,
            security_revision=1,
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
        self.dependencies = dependencies
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
        self.assertEqual(command.actor.principal_id, ADMINISTRATOR_ID)
        self.assertEqual(deployment_id, "local-trial")

    def test_invalid_keyset_cursor_is_a_bounded_bad_request(self) -> None:
        self._login()

        response = self.client.get(
            "/admin/organizations?after_display_name=Acme"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("page cursor is invalid", response.text)
        self.assertEqual(self.client.cookies[SERVER_CSRF_COOKIE_NAME], CSRF_TOKEN)

    def test_organization_administrator_is_bound_to_user_routes(self) -> None:
        self.authenticator.principal_kind = LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        self.authenticator.organization_id = "acme"

        login = self.client.post(
            "/login",
            data={
                "login_email": "administrator@acme.example.test",
                "password": "fictional-login-password",
            },
            headers={"Origin": "https://impodo.example.test"},
        )
        page = self.client.get("/admin/users")
        detail = self.client.get(f"/admin/users/{CUSTOMER_ID}")
        forbidden = self.client.get("/admin/organizations")

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/admin/users")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Fictional user", page.text)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("user@example.test", detail.text)
        self.assertEqual(forbidden.status_code, 403)
        query = self.organizations.user_queries[0]
        self.assertEqual(query.organization_id, "acme")
        self.assertEqual(query.actor.organization_id, "acme")

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

    def test_platform_provisioning_requires_confirmation_and_preserves_revision(self) -> None:
        self._login()
        view = OrganizationAdministrationView(
            organization=OrganizationView("acme", "Acme", OrganizationStatus.ACTIVE, 1, 7),
            administrator=None,
        )
        user = CustomerUserView(CUSTOMER_ID, "acme", "admin@acme.test", "Administrator", LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR, CustomerUserStatus.PENDING_ACTIVATION, 1, 1)
        with (
            patch.object(self.organizations, "get_organization", return_value=view, create=True),
            patch.object(self.organizations, "replace_organization_administrator", return_value=ProvisionedCustomerUserResult(user, "fictional-one-use-token", NOW + timedelta(hours=1)), create=True) as provision,
        ):
            page = self.client.get("/admin/organizations/acme")
            path = "/admin/organizations/acme/actions"
            values = ServerFormParser(page.text).form(path, operation="assign-administrator")
            values.update(display_name="Administrator", login_email="admin@acme.test")
            refused = self.client.post(path, data=dict(values, confirmation=""), headers={"Origin": "https://impodo.example.test"})
            self.assertEqual(refused.status_code, 400)
            provision.assert_not_called()
            result = self.client.post(path, data=values, headers={"Origin": "https://impodo.example.test"})
            self.assertEqual(result.status_code, 200)
            self.assertIn("/activate#fictional-one-use-token", result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(result.headers["referrer-policy"], "no-referrer")
            command = provision.call_args.args[0]
            self.assertEqual(command.expected_organization_revision, 7)
            self.assertEqual(command.actor.principal_id, ADMINISTRATOR_ID)

    def test_activation_needs_anonymous_csrf_and_never_echoes_invalid_secrets(self) -> None:
        with patch.object(self.organizations, "activate_customer_user", create=True) as activate:
            page = self.client.get("/activate")
            values = ServerFormParser(page.text).form("/activate")
            values.update(token="fictional-secret", password="fictional-long-password", password_confirmation="different")
            rejected = self.client.post("/activate", data=values, headers={"Origin": "https://impodo.example.test"})
            self.assertEqual(rejected.status_code, 400)
            self.assertNotIn("fictional-secret", rejected.text)
            self.assertNotIn("fictional-long-password", rejected.text)
            activate.assert_not_called()
            values.update(password_confirmation=values["password"], csrf_token="wrong")
            self.assertEqual(self.client.post("/activate", data=values, headers={"Origin": "https://impodo.example.test"}).status_code, 400)
            activate.assert_not_called()
            self.assertEqual(self.client.post("/activate", data=values, headers={"Origin": "https://other.example.test"}).status_code, 403)
            values["csrf_token"] = "invalid-unicode-\u00e9"
            self.assertEqual(self.client.post("/activate", data=values, headers={"Origin": "https://impodo.example.test"}).status_code, 400)

    def test_customer_login_reaches_account_and_cannot_open_administration(self) -> None:
        self.authenticator.principal_kind = LoginPrincipalKind.CUSTOMER_USER
        self.authenticator.organization_id = "acme"
        response = self.client.post("/login", data={"login_email": "user@example.test", "password": "fictional"}, headers={"Origin": "https://impodo.example.test"})
        self.assertEqual(response.headers["location"], "/account")
        self.assertEqual(self.client.get("/account").status_code, 200)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/admin/sessions").status_code, 403)
        self.assertEqual(self.client.get("/admin/history").status_code, 403)

    def test_login_and_token_forms_share_the_same_source_budget(self) -> None:
        dependencies = replace(self.dependencies, account_admission=ServerAccountAdmission(
            global_burst=10, source_burst=2, clock=lambda: 0.0))
        app = FastAPI()
        app.include_router(create_server_administration_router(dependencies))
        app.add_middleware(ServerBrowserSecurityMiddleware, public_url="https://impodo.example.test")
        with TestClient(app, base_url="https://impodo.example.test", follow_redirects=False) as client:
            headers = {"Origin": "https://impodo.example.test"}
            for _ in range(2):
                self.assertEqual(client.post("/login", headers=headers, data={"login_email": "operator@example.test", "password": "fictional"}).status_code, 303)
            self.assertEqual(client.post("/login", headers=headers, data={"login_email": "operator@example.test", "password": "fictional"}).status_code, 429)
            form = ServerFormParser(client.get("/activate").text).form("/activate")
            form.update(token="unissued", password="fictional-long-password", password_confirmation="fictional-long-password")
            self.assertEqual(client.post("/activate", headers=headers, data=form).status_code, 429)
        self.assertEqual(len(self.authenticator.commands), 2)

    def test_malformed_anonymous_cookie_is_rejected_on_every_public_form(self) -> None:
        for path in ("/activate", "/reset-password", "/forgot-password"):
            for cookie in (r'"invalid-\351"', "x" * 129):
                with self.subTest(path=path, cookie_length=len(cookie)):
                    response = self.client.post(
                        path,
                        data={"csrf_token": "invalid"},
                        headers={
                            "Origin": "https://impodo.example.test",
                            "Cookie": "__Host-impodo_account_csrf=" + cookie,
                        },
                    )
                    self.assertEqual(response.status_code, 400)

    def test_history_and_session_filters_use_only_session_organization(self) -> None:
        self.authenticator.principal_kind = LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        self.authenticator.organization_id = "acme"
        self._login()
        with (
            patch.object(self.organizations, "list_security_events", return_value=AdministrationEventPage((), None, None), create=True) as history,
            patch.object(self.organizations, "list_sessions", return_value=OrganizationSessionPage((), None, None), create=True) as sessions,
        ):
            self.assertEqual(self.client.get(f"/admin/history?organization_id=other&user_id={CUSTOMER_ID}").status_code, 200)
            self.assertEqual(self.client.get(f"/admin/sessions?organization_id=other&user_id={CUSTOMER_ID}").status_code, 200)
            for operation in (history, sessions):
                query = operation.call_args.args[0]
                self.assertEqual(query.organization_id, "acme")
                self.assertEqual(query.user_id, CUSTOMER_ID)
            self.assertEqual(self.client.get("/admin/history?before_occurred_at=bad").status_code, 400)

    def test_local_application_keeps_launch_session_and_has_no_server_account_routes(self) -> None:
        with TemporaryDirectory(prefix="impodo-local-identity-isolation-") as directory:
            app = create_local_app(Path(directory), secret_store=MemorySecretStore(), launch_token="fictional-launch-token")
            paths = {getattr(route, "path", "") for route in app.routes}
            self.assertTrue(paths.isdisjoint({"/login", "/logout", "/activate", "/reset-password", "/forgot-password", "/account", "/admin/users", "/admin/organizations"}))
            self.assertTrue(any("Session" in type(middleware).__name__ or "Session" in str(middleware) for middleware in app.user_middleware))


if __name__ == "__main__":
    unittest.main()
