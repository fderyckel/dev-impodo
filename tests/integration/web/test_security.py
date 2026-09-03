"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from unittest.mock import patch

from tests.support.browser_scenarios import (
    LocalBrowserSecurityTestCase,
    _csrf,
    uuid4,
)


class LocalBrowserSecurityTests(LocalBrowserSecurityTestCase):
    def test_browser_session_remains_valid_for_the_launcher_lifetime(self) -> None:
        launched_at = 1_000_000
        with patch("itsdangerous.timed.time.time", return_value=launched_at):
            launched = self.client.get(
                "/launch?token=launch-secret",
                follow_redirects=False,
            )

        self.assertEqual(launched.status_code, 303)
        cookie = launched.headers["set-cookie"].casefold()
        self.assertNotIn("max-age", cookie)
        self.assertNotIn("expires", cookie)

        with patch(
            "itsdangerous.timed.time.time",
            return_value=launched_at + (12 * 60 * 60),
        ):
            health = self.client.get("/health")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

    def test_launch_session_host_and_origin_controls(self) -> None:
        unauthenticated = self.client.get("/projects")
        self.assertEqual(unauthenticated.status_code, 401)

        wrong_host = self.client.get(
            "/launch?token=launch-secret",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(wrong_host.status_code, 400)

        launched = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)
        cookie = launched.headers["set-cookie"].casefold()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        reused = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(reused.status_code, 401)

        projects = self.client.get("/projects")
        self.assertEqual(projects.status_code, 200)
        self.assertIn(
            '<span class="brand-tagline">Prepare clean data for Odoo</span>',
            projects.text,
        )
        self.assertIn('id="app-sidebar"', projects.text)
        self.assertIn("data-sidebar-toggle", projects.text)
        self.assertIn('aria-label="Impodo workflow"', projects.text)
        self.assertIn("bootstrap-icons.svg#folder", projects.text)
        self.assertIn("Data remains on this computer.", projects.text)
        self.assertNotIn("Customer data remains on this computer.", projects.text)
        self.assertIn('class="creator-credit"', projects.text)
        self.assertIn("Made in", projects.text)
        self.assertIn("flag-luxembourg.svg", projects.text)
        self.assertEqual(projects.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", projects.headers["content-security-policy"])
        self.assertEqual(self.client.get("/projects").status_code, 200)
        self.assertEqual(self.client.get("/projects/new").status_code, 200)

        csrf = _csrf(projects.text)
        missing_origin = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "display_name": "Blocked",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
        )
        self.assertEqual(missing_origin.status_code, 403)

        origin_fallback = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "creation_request_id": str(uuid4()),
                "display_name": "Origin fallback",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(origin_fallback.status_code, 303)

        referer_fallback = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "creation_request_id": str(uuid4()),
                "display_name": "Referer fallback",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={"Referer": "http://testserver/projects/new"},
            follow_redirects=False,
        )
        self.assertEqual(referer_fallback.status_code, 303)

        cross_site = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "display_name": "Cross-site",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(cross_site.status_code, 403)

        hostile_referer = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "display_name": "Hostile",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers={"Referer": "http://testserver.attacker.example/projects/new"},
        )
        self.assertEqual(hostile_referer.status_code, 403)
