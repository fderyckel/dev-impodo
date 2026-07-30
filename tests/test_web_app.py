from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.worksheet.table import Table

from impodo.access import Actor, ActorIdentity, Capability
from impodo.connectors import MetadataSnapshot, RecordSnapshot
from impodo.local_stack import (
    LocalStackCheck,
    LocalStackService,
    LocalStackStartResult,
    LocalStackStatus,
    ReadinessLevel,
)
from impodo.models import (
    EnvironmentFingerprint,
    FieldMetadata,
    ModelMetadata,
    TargetRecord,
)
from impodo.projects import OdooConnectionMode, ProjectStatus, TargetEnvironment
from impodo.secrets import MemorySecretStore
from impodo.web import create_app
from impodo.workspace import SchemaOrigin, SourceSelection


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
        self.assertIn(
            '<span class="brand-tagline">Impodo - Import Anything into Odoo</span>',
            projects.text,
        )
        self.assertIn('id="app-sidebar"', projects.text)
        self.assertIn("data-sidebar-toggle", projects.text)
        self.assertIn('aria-label="Impodo workflow"', projects.text)
        self.assertIn("bootstrap-icons.svg#folder", projects.text)
        self.assertIn("Data remains on this computer.", projects.text)
        self.assertNotIn("Customer data remains on this computer.", projects.text)
        self.assertIn("Made in Luxembourg", projects.text)
        self.assertIn("flag-luxembourg.svg", projects.text)
        self.assertIn("by FdR", projects.text)
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

        origin_fallback = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "name": "Origin fallback",
                "source_system": "Other",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(origin_fallback.status_code, 303)

        referer_fallback = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "name": "Referer fallback",
                "source_system": "Other",
            },
            headers={"Referer": "http://testserver/projects/new"},
            follow_redirects=False,
        )
        self.assertEqual(referer_fallback.status_code, 303)

        cross_site = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "name": "Cross-site",
                "source_system": "Other",
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
                "name": "Hostile",
                "source_system": "Other",
            },
            headers={"Referer": "http://testserver.attacker.example/projects/new"},
        )
        self.assertEqual(hostile_referer.status_code, 403)


class LocalStackBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.workspace = Path(self.temporary.name) / "odoo_ve"
        self.config = self.workspace / "config" / "odoo.conf"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            "\n".join(
                (
                    "[options]",
                    "http_interface = 127.0.0.1",
                    "http_port = 18069",
                    "db_host = 127.0.0.1",
                    "db_port = 5544",
                    "db_user = odoo",
                    "db_name = odoo19_dev",
                    "db_password = postgres-secret",
                    "admin_passwd = master-secret",
                )
            ),
            encoding="utf-8",
        )
        for relative_path in (
            "tools/postgresql/pgsql/bin/pg_isready.exe",
            "tools/postgresql/pgsql/bin/pg_ctl.exe",
            "venv/Scripts/python.exe",
            "odoo/odoo-bin",
        ):
            candidate = self.workspace / relative_path
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.touch()
        (self.workspace / "pgdata").mkdir()
        (self.workspace / "logs").mkdir()
        self.picker_calls = 0
        self.start_calls = 0
        self.started_processes = []

        def pick_config():
            self.picker_calls += 1
            return self.config

        def probe(profile):
            return LocalStackStatus(
                config_path=str(profile.config_path),
                base_url=profile.base_url,
                database_hint=profile.database_hint,
                checks=(
                    LocalStackCheck(
                        "configuration",
                        "Configuration",
                        ReadinessLevel.READY,
                        "Valid loopback Odoo configuration.",
                    ),
                    LocalStackCheck(
                        "postgresql",
                        "PostgreSQL",
                        ReadinessLevel.READY,
                        "PostgreSQL is accepting connections.",
                    ),
                    LocalStackCheck(
                        "odoo",
                        "Odoo server",
                        ReadinessLevel.ACTION,
                        "Odoo is not responding yet.",
                    ),
                    LocalStackCheck(
                        "api",
                        "Impodo API",
                        ReadinessLevel.UNKNOWN,
                        "Use Save and test connection.",
                    ),
                ),
                profile=profile,
            )

        def starter(profile):
            self.start_calls += 1
            process = MagicMock()
            process.poll.return_value = None
            self.started_processes.append(process)
            return LocalStackStartResult(
                status=LocalStackStatus(
                    config_path=str(profile.config_path),
                    base_url=profile.base_url,
                    database_hint=profile.database_hint,
                    checks=(
                        LocalStackCheck(
                            "configuration",
                            "Configuration",
                            ReadinessLevel.READY,
                            "Valid loopback Odoo configuration.",
                        ),
                        LocalStackCheck(
                            "postgresql",
                            "PostgreSQL",
                            ReadinessLevel.READY,
                            "PostgreSQL is accepting connections.",
                        ),
                        LocalStackCheck(
                            "odoo",
                            "Odoo server",
                            ReadinessLevel.READY,
                            "Odoo 19.0 is responding.",
                        ),
                        LocalStackCheck(
                            "api",
                            "Impodo API",
                            ReadinessLevel.UNKNOWN,
                            "Use Save and test connection.",
                        ),
                    ),
                    profile=profile,
                ),
                odoo_process=process,
                postgresql_pid=None,
            )

        local_stack = LocalStackService(
            config_picker=pick_config,
            probe=probe,
            starter=starter,
        )
        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=MemorySecretStore(),
            local_stack_service=local_stack,
        )
        self.client = TestClient(self.app)
        launched = self.client.get(
            "/launch?token=launch-secret",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)
        projects = self.client.get("/projects")
        self.csrf = _csrf(projects.text)
        created = self.client.post(
            "/projects/new",
            data={
                "csrf_token": self.csrf,
                "name": "Local readiness",
                "source_system": "Other",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.project_id = created.headers["location"].split("/")[2]

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_selects_config_checks_status_and_keeps_profile_session_only(self) -> None:
        target = self.client.get(f"/projects/{self.project_id}/target")
        self.assertIn("Help me connect to local Odoo", target.text)
        self.assertIn("Choose a local odoo.conf file", target.text)

        selected = self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(selected.status_code, 303)
        self.assertEqual(
            selected.headers["location"],
            f"/projects/{self.project_id}/target?local_stack=1",
        )
        self.assertEqual(self.picker_calls, 1)

        refreshed = self.client.get(selected.headers["location"])
        self.assertIn("PostgreSQL is accepting connections", refreshed.text)
        self.assertIn("Odoo is not responding yet", refreshed.text)
        self.assertIn("status-ready", refreshed.text)
        self.assertIn("status-action", refreshed.text)
        self.assertIn('value="http://127.0.0.1:18069"', refreshed.text)
        self.assertIn('value="odoo19_dev"', refreshed.text)
        self.assertNotIn("postgres-secret", refreshed.text)
        self.assertNotIn("master-secret", refreshed.text)
        self.assertIn("Start PostgreSQL and Odoo", refreshed.text)

        project = self.app.state.context.repository.get(self.project_id)
        self.assertEqual(project.odoo_base_url, "")
        self.assertEqual(project.odoo_database, "")
        config_bytes = str(self.config).encode()
        for path in self.app.state.context.repository.project_directory(
            self.project_id
        ).rglob("*"):
            if path.is_file():
                self.assertNotIn(config_bytes, path.read_bytes())

    def test_start_requires_confirmation_and_updates_readiness(self) -> None:
        self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        unconfirmed = self.client.post(
            f"/projects/{self.project_id}/local-stack/start",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertIn("Confirm the detected paths", unconfirmed.text)
        self.assertEqual(self.start_calls, 0)

        started = self.client.post(
            f"/projects/{self.project_id}/local-stack/start",
            data={
                "csrf_token": self.csrf,
                "confirm_start": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(started.status_code, 303)
        self.assertEqual(self.start_calls, 1)
        page = self.client.get(started.headers["location"])
        self.assertIn("Odoo 19.0 is responding", page.text)
        self.assertIn("Control services started by Impodo", page.text)
        self.assertIn("Stop managed services", page.text)

    def test_stop_requires_confirmation_and_stops_only_managed_process(self) -> None:
        self._select_and_start_stack()
        process = self.started_processes[0]

        unconfirmed = self.client.post(
            f"/projects/{self.project_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "action": "stop",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertIn("Confirm control", unconfirmed.text)
        process.terminate.assert_not_called()

        stopped = self.client.post(
            f"/projects/{self.project_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "stop",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(stopped.status_code, 303)
        process.terminate.assert_called_once_with()
        page = self.client.get(stopped.headers["location"])
        self.assertIn("Start the missing local services", page.text)
        self.assertNotIn("Control services started by Impodo", page.text)

    def test_restart_stops_the_owned_process_then_starts_a_new_one(self) -> None:
        self._select_and_start_stack()
        first_process = self.started_processes[0]

        restarted = self.client.post(
            f"/projects/{self.project_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "restart",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(restarted.status_code, 303)
        first_process.terminate.assert_called_once_with()
        self.assertEqual(self.start_calls, 2)
        self.assertEqual(len(self.started_processes), 2)
        page = self.client.get(restarted.headers["location"])
        self.assertIn("Control services started by Impodo", page.text)

    def test_stop_requires_its_explicit_capability(self) -> None:
        self._select_and_start_stack()
        process = self.started_processes[0]
        self.app.state.context.actor = Actor(
            identity=ActorIdentity(
                issuer="https://identity.example.test",
                subject_id="stack-starter",
                display_name="Stack starter",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROJECT_EDIT,
                    Capability.LOCAL_STACK_INSPECT,
                    Capability.LOCAL_STACK_START,
                }
            ),
        )

        blocked = self.client.post(
            f"/projects/{self.project_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "stop",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 403)
        process.terminate.assert_not_called()

    def test_stop_never_controls_a_stack_started_outside_impodo(self) -> None:
        self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        blocked = self.client.post(
            f"/projects/{self.project_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "stop",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn("does not own any running service", blocked.text)
        self.assertEqual(self.start_calls, 0)

    def test_remote_project_cannot_open_local_assistant(self) -> None:
        saved = self.client.post(
            f"/projects/{self.project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "1",
                "odoo_connection_mode": "REMOTE",
                "target_environment": "TEST",
                "odoo_base_url": "https://odoo-test.example.com",
                "odoo_database": "odoo_test",
                "action": "save",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303)

        blocked = self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn(
            "available only in Local Odoo mode",
            blocked.text,
        )
        self.assertEqual(self.picker_calls, 0)

    def test_local_assistant_requires_its_explicit_capability(self) -> None:
        self.app.state.context.actor = Actor(
            identity=ActorIdentity(
                issuer="https://identity.example.test",
                subject_id="target-editor",
                display_name="Target editor",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROJECT_EDIT,
                }
            ),
        )

        blocked = self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.picker_calls, 0)

    def test_start_requires_its_explicit_capability(self) -> None:
        self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.app.state.context.actor = Actor(
            identity=ActorIdentity(
                issuer="https://identity.example.test",
                subject_id="stack-inspector",
                display_name="Stack inspector",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROJECT_EDIT,
                    Capability.LOCAL_STACK_INSPECT,
                }
            ),
        )

        blocked = self.client.post(
            f"/projects/{self.project_id}/local-stack/start",
            data={
                "csrf_token": self.csrf,
                "confirm_start": "1",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.start_calls, 0)

    def _select_and_start_stack(self) -> None:
        selected = self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(selected.status_code, 200)
        started = self.client.post(
            f"/projects/{self.project_id}/local-stack/start",
            data={
                "csrf_token": self.csrf,
                "confirm_start": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(started.status_code, 303)


class ProjectSetupWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.secrets = MemorySecretStore()
        self.connection_calls: list[tuple[str, str, OdooConnectionMode | None]] = []
        self.schema_calls: list[tuple[str, str]] = []
        self.model_catalog_calls: list[tuple[str, str]] = []

        def connection_tester(project, api_key):
            self.connection_calls.append(
                (project.project_id, api_key, project.odoo_connection_mode)
            )
            return "Read-only local connection succeeded: DEV / Odoo 19.4"

        def schema_reader(project, api_key):
            self.schema_calls.append((project.project_id, api_key))
            return _browser_schema()

        def model_catalog_reader(project, api_key):
            self.model_catalog_calls.append((project.project_id, api_key))
            return _browser_model_catalog()

        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=self.secrets,
            connection_tester=connection_tester,
            schema_reader=schema_reader,
            model_catalog_reader=model_catalog_reader,
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

    def test_local_schema_draft_does_not_call_the_odoo_api(self) -> None:
        context = self.app.state.context
        created = context.projects.create_project(
            actor=context.actor,
            name="Local draft",
            source_system="CSV",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            created,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            target_environment=TargetEnvironment.DEV,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_dev",
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            revision=2,
            updated_at=now,
            registered_at=now,
        )
        context.repository.save(
            registered,
            expected_revision=created.revision,
            event_type="TEST_PROJECT_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        context.repository.save_source_selection(
            registered.project_id,
            SourceSelection(
                selection_id=str(uuid4()),
                version=1,
                project_id=registered.project_id,
                created_at=now,
                created_by=context.actor.identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=context.actor,
        )

        drafted = self._post(
            f"/projects/{registered.project_id}/schema/local-draft",
            {
                "csrf_token": self.csrf,
                "acknowledge_local_draft": "1",
                "manual_model_label_0": "Contact",
                "manual_fields_0": "name | Name | char | yes | no",
            },
        )
        self.assertEqual(drafted.status_code, 303)
        self.assertEqual(self.schema_calls, [])
        schema = context.repository.get_odoo_schema_catalog(
            registered.project_id
        )
        self.assertIsNotNone(schema)
        self.assertEqual(schema.origin, SchemaOrigin.LOCAL_MANUAL)
        schema_page = self.client.get(drafted.headers["location"])
        self.assertIn("Unverified local draft", schema_page.text)
        self.assertIn("name | Name | char", schema_page.text)

    def test_complete_project_setup_registration_without_yaml(self) -> None:
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
                "description": "Project setup browser acceptance",
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
                "business_unit": "Example Business Unit",
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

        workbook_uploaded = self.client.post(
            f"/projects/{project_id}/files",
            data={"csrf_token": self.csrf, "revision": "4"},
            files={
                "source_file": (
                    "products.xlsx",
                    _workbook_bytes(),
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(workbook_uploaded.status_code, 303)

        target = self.client.post(
            f"/projects/{project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "5",
                "odoo_connection_mode": "LOCAL",
                "target_environment": "DEV",
                "odoo_base_url": "http://127.0.0.1:8069",
                "odoo_database": "odoo19_dev",
                "api_key": "super-secret-token",
                "intended_applications": "Contacts",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(target.status_code, 303)
        self.assertEqual(
            self.connection_calls,
            [
                (
                    project_id,
                    "super-secret-token",
                    OdooConnectionMode.LOCAL,
                )
            ],
        )
        target_page = self.client.get(target.headers["location"])
        self.assertIn("Read-only local connection succeeded", target_page.text)
        self.assertNotIn("super-secret-token", target_page.text)

        review = self.client.get(f"/projects/{project_id}/review")
        self.assertEqual(review.status_code, 200)
        self.assertNotIn("Complete these items", review.text)

        registered = self._post(
            f"/projects/{project_id}/register",
            {"csrf_token": self.csrf, "revision": "6"},
        )
        self.assertEqual(registered.status_code, 303)
        summary = self.client.get(registered.headers["location"])
        self.assertIn("Registered migration project", summary.text)
        self.assertIn("Inspect source data", summary.text)
        self.assertIn(
            f'href="/projects/{project_id}/sources"',
            summary.text,
        )
        project = self.app.state.context.repository.get(project_id)
        self.assertEqual(project.status, ProjectStatus.REGISTERED)
        self.assertEqual(
            project.odoo_connection_mode,
            OdooConnectionMode.LOCAL,
        )
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

        source_discovery = self.client.get(f"/projects/{project_id}/sources")
        self.assertEqual(source_discovery.status_code, 200)
        self.assertIn("Source discovery · Source inspection", source_discovery.text)
        self.assertIn("No source catalog yet", source_discovery.text)
        inspected = self.client.post(
            f"/projects/{project_id}/sources/inspect",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(inspected.status_code, 303)
        inspection_page = self.client.get(inspected.headers["location"])
        self.assertIn("Inspected 2 source file", inspection_page.text)
        self.assertIn("customers.csv", inspection_page.text)
        self.assertIn("C001", inspection_page.text)
        self.assertIn("products.xlsx", inspection_page.text)
        self.assertIn("ProductTable", inspection_page.text)
        self.assertIn("Candidate type", inspection_page.text)
        catalogs = self.app.state.context.repository.get_source_catalogs(project_id)
        self.assertEqual(len(catalogs), 2)
        self.assertEqual(catalogs[0].source_sha256, project.source_files[0].sha256)

        configured = self.client.post(
            f"/projects/{project_id}/sources/{catalogs[0].file_id}/configure",
            data={
                "csrf_token": self.csrf,
                "action": "confirm",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
                "selected_0": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(configured.status_code, 303)
        configured_page = self.client.get(configured.headers["location"])
        self.assertIn("Confirmed customers.csv", configured_page.text)

        workbook_configured = self.client.post(
            f"/projects/{project_id}/sources/{catalogs[1].file_id}/configure",
            data={
                "csrf_token": self.csrf,
                "action": "confirm",
                "encoding": "",
                "delimiter": "",
                "header_row_0": "1",
                "header_row_1": "1",
                "selected_1": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(workbook_configured.status_code, 303)
        configured_page = self.client.get(workbook_configured.headers["location"])
        self.assertIn("Confirmed products.xlsx", configured_page.text)
        self.assertIn("Choose and freeze datasets", configured_page.text)

        datasets = self.client.get(f"/projects/{project_id}/datasets")
        self.assertEqual(datasets.status_code, 200)
        self.assertIn("Source discovery · Dataset selection", datasets.text)
        self.assertIn("Freeze governed datasets", datasets.text)
        frozen = self.client.post(
            f"/projects/{project_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "customers",
                "dataset_name_1": "products",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303)
        self.assertEqual(
            frozen.headers["location"],
            f"/projects/{project_id}/schema",
        )

        project = self.app.state.context.repository.get(project_id)
        refreshed_models = self._post(
            f"/projects/{project_id}/schema/models/refresh",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(refreshed_models.status_code, 303)
        self.assertEqual(
            self.model_catalog_calls,
            [(project_id, "super-secret-token")],
        )
        model_page = self.client.get(refreshed_models.headers["location"])
        self.assertIn("Target schema · Permitted Odoo fields", model_page.text)
        self.assertIn("Choose target Odoo models", model_page.text)
        self.assertIn(
            "Project application focus: <strong>Contacts</strong>",
            model_page.text,
        )
        self.assertIn("Contact", model_page.text)
        self.assertIn("res.partner", model_page.text)
        self.assertIn(
            "Show models outside the project application focus",
            model_page.text,
        )

        rejected_scope = self.client.post(
            f"/projects/{project_id}/schema/scope",
            data={
                "csrf_token": self.csrf,
                "revision": str(project.revision),
                "permitted_models": "x.not.available",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected_scope.status_code, 422)
        self.assertIn(
            "not in the refreshed Odoo model catalogue",
            rejected_scope.text,
        )

        scope = self.client.post(
            f"/projects/{project_id}/schema/scope",
            data={
                "csrf_token": self.csrf,
                "revision": str(project.revision),
                "permitted_models": "res.partner",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(scope.status_code, 303)
        project = self.app.state.context.repository.get(project_id)
        self.assertEqual(project.intended_models, ("res.partner",))

        captured = self.client.post(
            f"/projects/{project_id}/schema/capture",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(captured.status_code, 303)
        self.assertEqual(self.schema_calls, [(project_id, "super-secret-token")])
        schema_page = self.client.get(captured.headers["location"])
        self.assertIn("Confirm target business keys", schema_page.text)
        self.assertIn("<h2>Contact <code>res.partner</code></h2>", schema_page.text)
        self.assertIn("Search fields", schema_page.text)
        self.assertIn("Show readonly and system fields", schema_page.text)
        governed = self.client.post(
            f"/projects/{project_id}/schema/govern",
            data={
                "csrf_token": self.csrf,
                "key_fields_0": "ref",
                "scope_fields_0": "",
                "key_description_0": "Unique contact reference",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(governed.status_code, 303)
        mapping_page = self.client.get(governed.headers["location"])
        self.assertIn(
            "Governed mapping · Map source columns to Odoo",
            mapping_page.text,
        )
        self.assertIn("Map source columns to Odoo", mapping_page.text)
        self.assertIn("res.partner::name", mapping_page.text)
        self.assertIn("Existing Odoo catalog", mapping_page.text)
        self.assertIn("inverse parent_id", mapping_page.text)
        self.assertIn("Source + fallback", mapping_page.text)
        self.assertIn("Leave unset / Odoo default", mapping_page.text)
        self.assertIn("Search scalar fields", mapping_page.text)
        self.assertIn("Preview", mapping_page.text)

        selection = (
            self.app.state.context.repository.get_source_selection(project_id)
        )
        schema_governance = (
            self.app.state.context.repository.get_schema_governance(project_id)
        )
        self.assertIsNotNone(selection)
        self.assertIsNotNone(schema_governance)
        customer, product = selection.datasets
        customer_code, customer_name = customer.columns
        product_code, product_name = product.columns
        business_key_id = schema_governance.business_keys[0].key_id
        submitted = self.client.post(
            f"/projects/{project_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "submit",
                "expected_parent_version": "",
                "target_model_0": "res.partner",
                "mode_0": "upsert",
                "source_identity_0": customer_code.stable_key,
                "business_key_0": business_key_id,
                "identity_source_0_0": customer_code.stable_key,
                "scalar_value_source_0_1": "source_with_fallback",
                "scalar_source_0_1": customer_name.stable_key,
                "scalar_literal_0_1": "Unnamed contact",
                "scalar_type_0_1": "string",
                "scalar_trim_0_1": "1",
                "scalar_collapse_0_1": "1",
                "scalar_empty_null_0_1": "1",
                "scalar_case_0_1": "preserve",
                "scalar_compare_0_1": "1",
                "scalar_null_0_1": "distinct",
                "target_model_1": "res.partner",
                "mode_1": "upsert",
                "source_identity_1": product_code.stable_key,
                "business_key_1": business_key_id,
                "identity_source_1_0": product_code.stable_key,
                "scalar_value_source_1_1": "constant",
                "scalar_literal_1_1": "Imported product",
                "scalar_type_1_1": "string",
                "scalar_case_1_1": "preserve",
                "scalar_compare_1_1": "1",
                "scalar_null_1_1": "distinct",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(submitted.status_code, 303)
        submitted_page = self.client.get(submitted.headers["location"])
        self.assertIn("Mapping submitted as version 1", submitted_page.text)
        self.assertIn("SUBMITTED", submitted_page.text)
        self.assertIn("valid", submitted_page.text.casefold())
        revision = (
            self.app.state.context.repository.get_mapping_revision(project_id)
        )
        revision_by_dataset = {
            item.dataset_id: item for item in revision.definition.datasets
        }
        self.assertEqual(
            revision_by_dataset[
                customer.dataset_id
            ].fields[0].value_source.value,
            "source_with_fallback",
        )
        self.assertTrue(
            revision_by_dataset[customer.dataset_id].fields[0].transform.trim
        )
        self.assertEqual(
            revision_by_dataset[
                product.dataset_id
            ].fields[0].value_source.value,
            "constant",
        )
        self.assertEqual(
            revision_by_dataset[product.dataset_id].fields[0].literal_value,
            "Imported product",
        )

        project = self.app.state.context.repository.get(project_id)
        changed_scope = self.client.post(
            f"/projects/{project_id}/schema/scope",
            data={
                "csrf_token": self.csrf,
                "revision": str(project.revision),
                "permitted_models": "res.company",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(changed_scope.status_code, 303)
        project = self.app.state.context.repository.get(project_id)
        self.assertEqual(project.intended_models, ("res.company",))
        self.assertIsNone(project.mapping_version)
        self.assertEqual(project.approval_status.value, "INVALIDATED")
        self.assertIsNone(
            self.app.state.context.repository.get_odoo_schema_catalog(project_id)
        )
        self.assertIsNone(
            self.app.state.context.repository.get_schema_governance(project_id)
        )
        self.assertIsNone(
            self.app.state.context.repository.get_mapping_revision(project_id)
        )

    def test_saved_key_is_not_reused_after_target_change(self) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "name": "Credential binding",
                "source_system": "Other",
            },
        )
        project_id = created.headers["location"].split("/")[2]
        local = self._post(
            f"/projects/{project_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": "1",
                "odoo_connection_mode": "LOCAL",
                "target_environment": "DEV",
                "odoo_base_url": "http://127.0.0.1:8069",
                "odoo_database": "odoo19_dev",
                "api_key": "local-only-key",
                "action": "test",
            },
        )
        self.assertEqual(local.status_code, 303)

        remote = self.client.post(
            f"/projects/{project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "2",
                "odoo_connection_mode": "REMOTE",
                "target_environment": "TEST",
                "odoo_base_url": "https://odoo-test.example.com",
                "odoo_database": "odoo_test",
                "action": "test",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(remote.status_code, 422)
        self.assertIn(
            "Enter an Odoo API key for this exact target",
            remote.text,
        )
        self.assertEqual(len(self.connection_calls), 1)
        self.assertEqual(self.connection_calls[0][1], "local-only-key")

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


def _browser_schema() -> MetadataSnapshot:
    return MetadataSnapshot(
        fingerprint=EnvironmentFingerprint(
            environment="DEV",
            database="odoo19_dev",
            odoo_version="19.0",
            snapshot_timestamp="2026-07-29T12:00:00Z",
            module_versions={"base": "19.0.1.0"},
        ),
        models={
            "res.partner": ModelMetadata(
                model="res.partner",
                description=None,
                fields={
                    "name": FieldMetadata(
                        name="name",
                        type="char",
                        label="Name",
                        required=True,
                    ),
                    "ref": FieldMetadata(
                        name="ref",
                        type="char",
                        label="Reference",
                    ),
                    "display_name": FieldMetadata(
                        name="display_name",
                        type="char",
                        label="Display Name",
                        readonly=True,
                    ),
                    "company_id": FieldMetadata(
                        name="company_id",
                        type="many2one",
                        label="Company",
                        relation="res.company",
                    ),
                    "category_ids": FieldMetadata(
                        name="category_ids",
                        type="many2many",
                        label="Tags",
                        relation="res.partner.category",
                    ),
                    "child_ids": FieldMetadata(
                        name="child_ids",
                        type="one2many",
                        label="Contacts",
                        relation="res.partner",
                        relation_field="parent_id",
                    ),
                },
            )
        },
    )


def _browser_model_catalog() -> RecordSnapshot:
    fingerprint = EnvironmentFingerprint(
        environment="DEV",
        database="odoo19_dev",
        odoo_version="19.0",
        snapshot_timestamp="2026-07-30T12:00:00Z",
        module_versions={"base": "19.0.1.0"},
    )
    fields = (
        "name",
        "model",
        "abstract",
        "transient",
        "modules",
        "state",
    )
    return RecordSnapshot(
        fingerprint=fingerprint,
        records={
            "ir.model": (
                TargetRecord(
                    model="ir.model",
                    odoo_id=1,
                    values={
                        "name": "Contact",
                        "model": "res.partner",
                        "abstract": False,
                        "transient": False,
                        "modules": "base, contacts",
                        "state": "base",
                    },
                ),
                TargetRecord(
                    model="ir.model",
                    odoo_id=2,
                    values={
                        "name": "Product",
                        "model": "product.template",
                        "abstract": False,
                        "transient": False,
                        "modules": "product, stock",
                        "state": "base",
                    },
                ),
                TargetRecord(
                    model="ir.model",
                    odoo_id=3,
                    values={
                        "name": "Company",
                        "model": "res.company",
                        "abstract": False,
                        "transient": False,
                        "modules": "base",
                        "state": "base",
                    },
                ),
            )
        },
        requested_fields={"ir.model": fields},
        complete=True,
    )


def _workbook_bytes() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Products"
    worksheet.append(["Code", "Name"])
    worksheet.append(["P001", "Example product"])
    worksheet.add_table(Table(displayName="ProductTable", ref="A1:B2"))
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
