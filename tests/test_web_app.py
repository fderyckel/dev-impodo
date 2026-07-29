from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from uc_migration_profiler.projects import ProjectStatus
from uc_migration_profiler.secrets import MemorySecretStore
from uc_migration_profiler.web import create_app


ROOT = Path(__file__).resolve().parents[1]
POST_HEADERS = {
    "Origin": "http://testserver",
    "Sec-Fetch-Site": "same-origin",
}


class LocalBrowserSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=MemorySecretStore(),
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

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
        self.assertEqual(projects.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", projects.headers["content-security-policy"])

        csrf = _csrf(projects.text)
        missing_origin = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "name": "Blocked",
                "source_system": "Other",
            },
        )
        self.assertEqual(missing_origin.status_code, 403)


class PhaseAWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.secrets = MemorySecretStore()
        self.connection_calls: list[tuple[str, str]] = []

        def connection_tester(project, api_key):
            self.connection_calls.append((project.project_id, api_key))
            return "Read-only connection succeeded: TEST / Odoo 19.4"

        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=self.secrets,
            connection_tester=connection_tester,
        )
        self.client = TestClient(self.app)
        launched = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)
        self.csrf = _csrf(self.client.get("/projects").text)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_complete_phase_a_registration_without_yaml(self) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "name": "Customer migration",
                "source_system": "Dynamics AX 2012",
            },
        )
        self.assertEqual(created.status_code, 303)
        project_id = created.headers["location"].split("/")[2]

        details = self._post(
            f"/projects/{project_id}/details",
            {
                "csrf_token": self.csrf,
                "revision": "1",
                "name": "Customer migration",
                "source_system": "Dynamics AX 2012",
                "export_status": "RECEIVED",
                "export_date": date.today().isoformat(),
                "description": "Phase A browser acceptance",
            },
        )
        self.assertEqual(details.headers["location"], f"/projects/{project_id}/governance")

        governance = self._post(
            f"/projects/{project_id}/governance",
            {
                "csrf_token": self.csrf,
                "revision": "2",
                "data_manager": "Data Manager",
                "functional_owner": "Functional Owner",
                "business_unit": "UC",
                "data_classification": "CONFIDENTIAL",
                "retention_days": "90",
            },
        )
        self.assertEqual(governance.status_code, 303)

        uploaded = self.client.post(
            f"/projects/{project_id}/files",
            data={"csrf_token": self.csrf, "revision": "3"},
            files={
                "source_file": (
                    "customers.csv",
                    b"code,name\nC001,Example\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)

        target = self.client.post(
            f"/projects/{project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "4",
                "target_environment": "TEST",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "uc_test",
                "intended_applications": ["Contacts"],
                "intended_models": "res.partner",
                "api_key": "super-secret-token",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(target.status_code, 303)
        self.assertEqual(
            self.connection_calls,
            [(project_id, "super-secret-token")],
        )
        target_page = self.client.get(target.headers["location"])
        self.assertIn("Read-only connection succeeded", target_page.text)
        self.assertNotIn("super-secret-token", target_page.text)

        review = self.client.get(f"/projects/{project_id}/review")
        self.assertEqual(review.status_code, 200)
        self.assertNotIn("Complete these items", review.text)

        registered = self._post(
            f"/projects/{project_id}/register",
            {"csrf_token": self.csrf, "revision": "5"},
        )
        self.assertEqual(registered.status_code, 303)
        summary = self.client.get(registered.headers["location"])
        self.assertIn("Registered migration project", summary.text)
        project = self.app.state.context.repository.get(project_id)
        self.assertEqual(project.status, ProjectStatus.REGISTERED)
        self.assertEqual(project.mapping_version, None)
        self.assertNotIn(
            b"super-secret-token",
            (
                self.app.state.context.repository.project_directory(project_id)
                / "project.duckdb"
            ).read_bytes(),
        )
        manifest = (
            self.app.state.context.repository.project_directory(project_id)
            / "audit"
            / f"project-registration-r{project.revision}.json"
        )
        self.assertTrue(manifest.is_file())
        self.assertNotIn("super-secret-token", manifest.read_text())

    def _post(self, path: str, data: dict[str, str]):
        return self.client.post(
            path,
            data=data,
            headers=POST_HEADERS,
            follow_redirects=False,
        )


def _csrf(html: str) -> str:
    matched = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if matched is None:
        raise AssertionError("CSRF token not found")
    return matched.group(1)
