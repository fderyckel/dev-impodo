"""Verify fail-closed browser transport and session-cookie policy."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    CurrentSessionView,
    ServerSessionCredentials,
    SessionAuthenticationMethod,
    SessionSubject,
)
from impodo.web.composition.server_session_http import (
    SERVER_CSRF_COOKIE_NAME,
    SERVER_SESSION_COOKIE_NAME,
    ServerBrowserSecurityMiddleware,
    clear_server_session_cookie,
    set_server_session_cookie,
)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)


def _credentials() -> ServerSessionCredentials:
    return ServerSessionCredentials(
        handle="20000000-0000-4000-8000-000000000002.opaque-secret",
        csrf_token="independent-csrf-secret",
        session=CurrentSessionView(
            session_id="20000000-0000-4000-8000-000000000002",
            subject=SessionSubject(
                principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
                principal_id="10000000-0000-4000-8000-000000000001",
                organization_id=None,
            ),
            authentication_method=SessionAuthenticationMethod.PASSWORD,
            security_revision=3,
            created_at=NOW,
            last_seen_at=NOW,
            idle_expires_at=NOW + timedelta(minutes=30),
            absolute_expires_at=NOW + timedelta(hours=12),
            elevated_until=None,
            optimistic_revision=1,
        ),
    )


class ServerSessionHttpTests(unittest.TestCase):
    def test_cookie_is_host_only_secure_http_only_and_same_site(self) -> None:
        response = JSONResponse({"status": "ok"})

        set_server_session_cookie(
            response,
            _credentials(),
            absolute_timeout=timedelta(hours=12),
        )

        cookies = response.headers.getlist("set-cookie")
        session_cookie = next(
            value
            for value in cookies
            if value.startswith(f"{SERVER_SESSION_COOKIE_NAME}=")
        )
        csrf_cookie = next(
            value
            for value in cookies
            if value.startswith(f"{SERVER_CSRF_COOKIE_NAME}=")
        )
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("Max-Age=43200", session_cookie)
        self.assertIn("Path=/", session_cookie)
        self.assertIn("SameSite=lax", session_cookie)
        self.assertIn("Secure", session_cookie)
        self.assertNotIn("Domain=", session_cookie)
        self.assertNotIn(_credentials().csrf_token, session_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        self.assertIn(_credentials().csrf_token, csrf_cookie)
        self.assertIn("Secure", csrf_cookie)

        cleared = JSONResponse({"status": "ok"})
        clear_server_session_cookie(cleared)
        cleared_cookies = cleared.headers.getlist("set-cookie")
        self.assertEqual(len(cleared_cookies), 2)
        self.assertTrue(
            all("Max-Age=0" in value and "Secure" in value for value in cleared_cookies)
        )

    def test_middleware_rejects_wrong_host_and_cross_origin_unsafe_requests(self) -> None:
        app = FastAPI()

        @app.api_route("/action", methods=["GET", "POST"])
        async def action() -> dict[str, str]:
            return {"status": "ok"}

        app.add_middleware(
            ServerBrowserSecurityMiddleware,
            public_url="https://impodo.example.test",
        )

        with TestClient(app, base_url="https://impodo.example.test") as client:
            accepted = client.get("/action")
            missing_origin = client.post("/action")
            wrong_origin = client.post(
                "/action",
                headers={"Origin": "https://attacker.example"},
            )
            accepted_unsafe = client.post(
                "/action",
                headers={"Origin": "https://impodo.example.test"},
            )
            wrong_host = client.get(
                "/action",
                headers={"Host": "attacker.example"},
            )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(missing_origin.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(accepted_unsafe.status_code, 200)
        self.assertEqual(wrong_host.status_code, 400)
        for response in (
            accepted,
            missing_origin,
            wrong_origin,
            accepted_unsafe,
            wrong_host,
        ):
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
