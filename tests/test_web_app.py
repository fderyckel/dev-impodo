from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from html import unescape
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.worksheet.table import Table

from impodo.access import Actor, ActorIdentity, Capability
from impodo.connectors import ConnectorError, MetadataSnapshot, RecordSnapshot
from impodo.local_odoo_reader import LocalOdooMetadataReader
from impodo.local_stack import (
    LocalStackCheck,
    LocalStackService,
    LocalStackStartResult,
    LocalStackStatus,
    ReadinessLevel,
)
from impodo.mapping_semantics import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    SchemaGovernance,
    ValueMapping,
)
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.projects import OdooConnectionMode, ProjectStatus
from impodo.secrets import MemorySecretStore
from impodo.web import create_app
from impodo.web.app import _source_value_choices
from impodo.workspace import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


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
                    "db_name = odoo19_local",
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
        self.stack_running = False
        self.started_processes = []

        def pick_config():
            self.picker_calls += 1
            return self.config

        def probe(profile):
            odoo_ready = self.stack_running
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
                        (
                            ReadinessLevel.READY
                            if odoo_ready
                            else ReadinessLevel.ACTION
                        ),
                        (
                            "Odoo 19.0 is responding."
                            if odoo_ready
                            else "Odoo is not responding yet."
                        ),
                    ),
                    LocalStackCheck(
                        "api",
                        "Database access (read-only)",
                        ReadinessLevel.UNKNOWN,
                        "Use Save and test connection.",
                    ),
                ),
                profile=profile,
            )

        def starter(profile):
            self.start_calls += 1
            self.stack_running = True
            process = MagicMock()
            process.poll.return_value = None
            process.terminate.side_effect = lambda: setattr(
                self,
                "stack_running",
                False,
            )
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
                            "Database access (read-only)",
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
        self.local_odoo_reader = MagicMock(spec=LocalOdooMetadataReader)
        self.local_odoo_reader.get_target_fingerprint.side_effect = (
            lambda project, _profile: _browser_schema(project).fingerprint
        )
        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=MemorySecretStore(),
            local_stack_service=local_stack,
            local_odoo_reader=self.local_odoo_reader,
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
        self.assertIn('value="odoo19_local"', refreshed.text)
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

    def test_local_connection_test_opens_all_green_results(self) -> None:
        self._select_and_start_stack()

        tested = self.client.post(
            f"/projects/{self.project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "1",
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:18069",
                "odoo_database": "odoo19_local",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(tested.status_code, 303)
        self.assertEqual(
            tested.headers["location"],
            f"/projects/{self.project_id}/target?local_stack=1",
        )
        results = self.client.get(tested.headers["location"])
        self.assertIn('data-auto-open="true"', results.text)
        self.assertEqual(results.text.count("status-ready"), 4)
        self.assertIn("Database access (read-only)", results.text)
        self.assertIn("Read-only database access succeeded", results.text)
        self.local_odoo_reader.get_target_fingerprint.assert_called_once()

    def test_local_connection_test_opens_mixed_failure_results(self) -> None:
        self.client.post(
            f"/projects/{self.project_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        tested = self.client.post(
            f"/projects/{self.project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "1",
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:18069",
                "odoo_database": "odoo19_local",
                "action": "test",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(tested.status_code, 422)
        self.assertIn('data-auto-open="true"', tested.text)
        self.assertEqual(tested.text.count("status-ready"), 2)
        self.assertEqual(tested.text.count("status-error"), 2)
        self.assertIn("Local connection checks failed: Odoo server", tested.text)
        self.assertIn("Read-only database access failed", tested.text)
        self.local_odoo_reader.get_target_fingerprint.assert_not_called()

    def test_local_connection_test_marks_database_access_failure(self) -> None:
        self._select_and_start_stack()
        self.local_odoo_reader.get_target_fingerprint.side_effect = ConnectorError(
            "The configured database could not be opened."
        )

        tested = self.client.post(
            f"/projects/{self.project_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": "1",
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:18069",
                "odoo_database": "odoo19_local",
                "action": "test",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(tested.status_code, 422)
        self.assertIn('data-auto-open="true"', tested.text)
        self.assertEqual(tested.text.count("status-ready"), 3)
        self.assertEqual(tested.text.count("status-error"), 1)
        self.assertIn("The configured database could not be opened", tested.text)
        self.assertIn("Read-only database access failed", tested.text)

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
                "odoo_base_url": "https://odoo.example.com",
                "odoo_database": "odoo_review",
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
        self.readiness_calls = []
        self.local_odoo_reader = MagicMock(spec=LocalOdooMetadataReader)

        def connection_tester(project, api_key):
            self.connection_calls.append(
                (project.project_id, api_key, project.odoo_connection_mode)
            )
            return "Read-only local connection succeeded: migration / Odoo 19.4"

        def schema_reader(project, api_key):
            self.schema_calls.append((project.project_id, api_key))
            return _browser_schema(project)

        def model_catalog_reader(project, api_key):
            self.model_catalog_calls.append((project.project_id, api_key))
            return _browser_model_catalog(project)

        def readiness_reader(project, metadata_requests, record_requests):
            self.readiness_calls.append(
                (project.project_id, metadata_requests, record_requests)
            )
            metadata = _browser_schema(project)
            records = RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={item.model: () for item in record_requests},
                requested_fields={
                    item.model: item.fields for item in record_requests
                },
            )
            return metadata, records

        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=self.secrets,
            connection_tester=connection_tester,
            schema_reader=schema_reader,
            model_catalog_reader=model_catalog_reader,
            readiness_reader=readiness_reader,
            local_odoo_reader=self.local_odoo_reader,
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

    def test_project_list_permanently_deletes_project_after_confirmation(
        self,
    ) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "name": "Disposable rehearsal",
                "source_system": "Other",
            },
        )
        project_id = created.headers["location"].split("/")[2]
        project = self.app.state.context.repository.get(project_id)
        targeted = self._post(
            f"/projects/{project_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": str(project.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "migration",
                "intended_applications": "Contacts",
                "api_key": "disposable-api-key",
                "remember_api_key": "1",
                "action": "save",
            },
        )
        self.assertEqual(targeted.status_code, 303)
        self.assertEqual(len(self.secrets.values), 1)

        project = self.app.state.context.repository.get(project_id)
        project_dir = self.app.state.context.repository.project_directory(project_id)
        project_list = self.client.get("/projects")
        self.assertIn(
            f'action="/projects/{project_id}/delete"',
            project_list.text,
        )
        self.assertIn('data-project-delete-dialog', project_list.text)
        self.assertIn('data-project-delete-trigger', project_list.text)
        self.assertIn('bootstrap-icons.svg#trash3', project_list.text)
        self.assertIn(
            "This deletes the project, uploaded files, mappings, reports, and audit",
            project_list.text,
        )
        self.assertIn("This cannot be undone.", project_list.text)
        self.assertNotIn("does not change Odoo", project_list.text)
        self.assertNotIn(
            "Records already created or updated in Odoo will remain",
            project_list.text,
        )

        stale = self._post(
            f"/projects/{project_id}/delete",
            {
                "csrf_token": self.csrf,
                "revision": str(project.revision - 1),
            },
        )
        self.assertEqual(stale.status_code, 422)
        self.assertTrue(project_dir.is_dir())
        self.assertEqual(len(self.secrets.values), 1)

        deleted = self._post(
            f"/projects/{project_id}/delete",
            {
                "csrf_token": self.csrf,
                "revision": str(project.revision),
            },
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(deleted.headers["location"], "/projects")
        self.assertFalse(project_dir.exists())
        self.assertEqual(self.secrets.values, {})
        missing = self.client.get(f"/projects/{project_id}")
        self.assertEqual(missing.status_code, 404)
        refreshed = self.client.get(deleted.headers["location"])
        self.assertIn(
            'Deleted project "Disposable rehearsal".',
            unescape(refreshed.text),
        )

        script = self.client.get("/static/app.js")
        self.assertIn("projectDeleteDialog.showModal()", script.text)
        self.assertIn("form?.requestSubmit()", script.text)

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
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
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

    def test_registered_local_schema_uses_selected_config_without_api_key(
        self,
    ) -> None:
        context = self.app.state.context
        created = context.projects.create_project(
            actor=context.actor,
            name="Keyless local schema",
            source_system="CSV",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            created,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:18069",
            odoo_database="odoo19_local",
            intended_applications=("Contacts",),
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
                content_hash="sha256:" + "b" * 64,
            ),
            actor=context.actor,
        )
        unconfigured_page = self.client.get(
            f"/projects/{registered.project_id}/schema"
        )
        self.assertIn(
            "Choose the local Odoo configuration first",
            unconfigured_page.text,
        )
        self.assertIn(
            f'action="/projects/{registered.project_id}/schema/local-config"',
            unconfigured_page.text,
        )
        blocked = self.client.post(
            f"/projects/{registered.project_id}/schema/models/refresh",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("Local mode does not require an API key", blocked.text)

        workspace = Path(self.temporary.name) / "local-odoo"
        config = workspace / "config" / "odoo.conf"
        config.parent.mkdir(parents=True)
        config.write_text(
            "\n".join(
                (
                    "[options]",
                    "http_interface = 127.0.0.1",
                    "http_port = 18069",
                    "db_host = 127.0.0.1",
                    "db_port = 5544",
                    "db_user = odoo",
                    "db_name = odoo19_local",
                )
            ),
            encoding="utf-8",
        )
        for relative_path in (
            "venv/Scripts/python.exe",
            "odoo/odoo-bin",
        ):
            executable = workspace / relative_path
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
        status = context.local_stack.select_config(
            registered.project_id,
            config,
        )
        self.assertIsNotNone(status.profile)
        configured_local_stack = context.local_stack

        self.local_odoo_reader.get_model_catalog.return_value = (
            _browser_model_catalog(registered)
        )
        self.local_odoo_reader.get_model_metadata.return_value = (
            _browser_schema(registered)
        )

        page = self.client.get(f"/projects/{registered.project_id}/schema")
        self.assertIn("Local Odoo configuration is ready", page.text)
        self.assertIn("No Odoo API key is required", page.text)
        self.assertIn("Load Odoo record types", page.text)
        self.assertNotIn("Verify access and load models", page.text)
        refreshed = self._post(
            f"/projects/{registered.project_id}/schema/models/refresh",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(refreshed.status_code, 303)
        self.local_odoo_reader.get_model_catalog.assert_called_once()
        self.assertEqual(self.model_catalog_calls, [])
        verified_page = self.client.get(refreshed.headers["location"])
        self.assertIn("Odoo record types are ready", verified_page.text)
        self.assertIn(
            "Live local metadata access was also verified",
            verified_page.text,
        )
        self.assertIn("Refresh Odoo record types", verified_page.text)
        context.local_stack = LocalStackService()
        cached_page = self.client.get(
            f"/projects/{registered.project_id}/schema"
        )
        self.assertIn("Odoo record types are ready", cached_page.text)
        self.assertIn(
            "Technical evidence",
            cached_page.text,
        )
        self.assertIn("Live connection not checked this session", cached_page.text)
        self.assertIn("res.partner", cached_page.text)
        self.local_odoo_reader.get_model_catalog.assert_called_once()

        project = context.repository.get(registered.project_id)
        scoped = self._post(
            f"/projects/{registered.project_id}/schema/scope",
            {
                "csrf_token": self.csrf,
                "revision": str(project.revision),
                "permitted_models": "res.partner",
            },
        )
        self.assertEqual(scoped.status_code, 303)
        self.local_odoo_reader.get_model_catalog.assert_called_once()
        context.local_stack = configured_local_stack
        scoped_page = self.client.get(scoped.headers["location"])
        self.assertIn(
            "Load fields for selected record types",
            scoped_page.text,
        )
        captured = self._post(
            f"/projects/{registered.project_id}/schema/capture",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(captured.status_code, 303)
        self.local_odoo_reader.get_model_metadata.assert_called_once()
        metadata_call = (
            self.local_odoo_reader.get_model_metadata.call_args.args
        )
        self.assertEqual(metadata_call[2], ("res.partner",))
        self.assertEqual(self.schema_calls, [])
        context.local_stack = LocalStackService()
        cached_schema_page = self.client.get(
            f"/projects/{registered.project_id}/schema"
        )
        self.assertIn(
            "Odoo fields are ready",
            cached_schema_page.text,
        )
        self.assertIn(
            "The snapshot includes inherited fields and is used without another Odoo call",
            cached_schema_page.text,
        )
        self.assertIn(
            "Refresh fields for selected record types",
            cached_schema_page.text,
        )
        self.local_odoo_reader.get_model_metadata.assert_called_once()

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
                "odoo_base_url": "http://127.0.0.1:8069",
                "odoo_database": "odoo19_local",
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
        self.assertIn("Setup complete", summary.text)
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
        self.assertIn("Step 1 · Source data", source_discovery.text)
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
        self.assertIn("Choose tables", configured_page.text)

        datasets = self.client.get(f"/projects/{project_id}/datasets")
        self.assertEqual(datasets.status_code, 200)
        self.assertIn("Step 2 · Select tables", datasets.text)
        self.assertIn("Choose the tables to prepare", datasets.text)
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
            f"/projects/{project_id}/derived-entities",
        )
        derived_page = self.client.get(frozen.headers["location"])
        self.assertIn("Prepare related records", derived_page.text)
        self.assertIn(
            "Saved rules are repeated consistently for every row",
            derived_page.text,
        )
        selection = (
            self.app.state.context.repository.get_source_selection(project_id)
        )
        self.assertIsNotNone(selection)
        source_choices = _source_value_choices(
            self.app.state.context,
            project_id,
            selection.datasets[0].dataset_id,
            selection.datasets[0].columns[0].stable_key,
        )
        self.assertEqual(source_choices, ({"value": "C001", "count": 1},))
        product_name = selection.datasets[1].columns[1]
        product_code = selection.datasets[1].columns[0]
        related_preview = self.client.post(
            f"/projects/{project_id}/derived-entities/related/preview",
            data={
                "csrf_token": self.csrf,
                "expected_parent_version": "",
                "source_dataset_id": selection.datasets[1].dataset_id,
                "parent_dataset_name": "product_groups",
                "child_dataset_name": "product_rows",
                "parent_key_column_key": product_name.stable_key,
                "scope_column_key": "",
                "child_key_column_key": product_code.stable_key,
                "blank_policy": "block",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(related_preview.status_code, 200)
        self.assertIn("Review before creating", related_preview.text)
        self.assertIn("Create these related datasets", related_preview.text)
        saved_related = self.client.post(
            f"/projects/{project_id}/derived-entities/related/save",
            data={
                "csrf_token": self.csrf,
                "expected_parent_version": "",
                "source_dataset_id": selection.datasets[1].dataset_id,
                "parent_dataset_name": "product_groups",
                "child_dataset_name": "product_rows",
                "parent_key_column_key": product_name.stable_key,
                "scope_column_key": "",
                "child_key_column_key": product_code.stable_key,
                "blank_policy": "block",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved_related.status_code, 303)
        related_page = self.client.get(saved_related.headers["location"])
        self.assertIn(
            "Created related datasets product_groups and product_rows",
            related_page.text,
        )
        related_plan = (
            self.app.state.context.repository.get_derived_entity_plan(project_id)
        )
        self.assertIsNotNone(related_plan)
        removed_related = self.client.post(
            (
                f"/projects/{project_id}/derived-entities/"
                f"{related_plan.rules[0].rule_id}/delete"
            ),
            data={
                "csrf_token": self.csrf,
                "expected_parent_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(removed_related.status_code, 303)
        derived_rule_data = {
            "csrf_token": self.csrf,
            "expected_parent_version": "2",
            "source_binding": (
                f"{selection.datasets[1].dataset_id}|{product_name.stable_key}"
            ),
            "output_dataset_name": "product_names",
            "target_model": "res.partner",
            "target_name_field": "name",
            "external_id_namespace": "dynamics_ax_2012",
            "parent_separator": "",
            "blank_policy": "block",
        }
        lookup_preview = self.client.post(
            f"/projects/{project_id}/derived-entities/lookup/preview",
            data=derived_rule_data,
            headers=POST_HEADERS,
        )
        self.assertEqual(lookup_preview.status_code, 200)
        self.assertIn("Review before creating", lookup_preview.text)
        self.assertIn("Create this related dataset", lookup_preview.text)
        saved_derived = self.client.post(
            f"/projects/{project_id}/derived-entities/save",
            data=derived_rule_data,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved_derived.status_code, 303)
        derived_preview = self.client.get(saved_derived.headers["location"])
        self.assertIn("Saved derived dataset product_names", derived_preview.text)
        self.assertIn("Example product", derived_preview.text)
        self.assertIn("impodo_dynamics_ax_2012.res_partner_", derived_preview.text)
        self.assertIn(
            "available in Mapping beside the original rows",
            derived_preview.text,
        )
        self.assertNotIn("entity:P001", derived_preview.text)

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
        self.assertIn("Step 3 · Odoo fields", model_page.text)
        self.assertIn("Choose Odoo record types", model_page.text)
        self.assertIn(
            "Project application focus: <strong>Contacts</strong>",
            model_page.text,
        )
        self.assertIn("Contact", model_page.text)
        self.assertIn("res.partner", model_page.text)
        self.assertIn(
            "Show all record types",
            model_page.text,
        )
        self.assertIn(
            f'action="/projects/{project_id}/schema"',
            model_page.text,
        )
        self.assertIn('aria-live="polite"', model_page.text)
        self.assertIn(
            'data-model-search-text="product product.template product stock"',
            model_page.text,
        )

        scope_alias = self.client.get(
            f"/projects/{project_id}/schema/scope",
            follow_redirects=False,
        )
        self.assertEqual(scope_alias.status_code, 303)
        self.assertEqual(
            scope_alias.headers["location"],
            f"/projects/{project_id}/schema",
        )

        model_picker_script = self.client.get("/static/app.js")
        self.assertIn("const hasQuery = Boolean(query);", model_picker_script.text)
        self.assertIn(
            "matches && (hasQuery || browseAll || choice.inFocus || selected)",
            model_picker_script.text,
        )
        model_picker_styles = self.client.get("/static/app.css")
        self.assertIn("label.model-choice[hidden]", model_picker_styles.text)

        rejected_scope = self.client.post(
            f"/projects/{project_id}/schema",
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
            f"/projects/{project_id}/schema",
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
        self.assertIn("Tell Impodo how to find existing records", schema_page.text)
        self.assertIn("<h2>Contact <code>res.partner</code></h2>", schema_page.text)
        self.assertIn("Search fields", schema_page.text)
        self.assertIn("Show readonly and system fields", schema_page.text)
        self.assertIn("Impodo found no single safe recommendation", schema_page.text)
        self.assertIn("Reference (ref)", schema_page.text)
        self.assertIn("Combined key or technical entry", schema_page.text)
        governed = self.client.post(
            f"/projects/{project_id}/schema/govern",
            data={
                "csrf_token": self.csrf,
                "primary_key_field_0": "ref",
                "primary_scope_field_0": "",
                "key_fields_0": "",
                "scope_fields_0": "",
                "key_description_0": "Unique contact reference",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(governed.status_code, 303)
        mapping_page = self.client.get(governed.headers["location"])
        self.assertIn("<p class=\"eyebrow\">Step 4 · Match fields</p>", mapping_page.text)
        self.assertIn("Match your data to Odoo", mapping_page.text)
        self.assertIn('<details class="technical-evidence">', mapping_page.text)
        self.assertNotIn("Evidence binding", mapping_page.text)
        self.assertIn("How do we identify each source row?", mapping_page.text)
        self.assertIn("How do we find the matching Odoo record?", mapping_page.text)
        self.assertIn('class="identity-pair"', mapping_page.text)
        self.assertIn("res.partner::name", mapping_page.text)
        self.assertIn("Existing Odoo records", mapping_page.text)
        self.assertIn("inverse parent_id", mapping_page.text)
        self.assertIn("Source + fallback", mapping_page.text)
        self.assertIn("Leave unset / Odoo default", mapping_page.text)
        self.assertIn("Fields to fill in Odoo", mapping_page.text)
        self.assertNotIn("Scalar target fields", mapping_page.text)
        self.assertIn("Find an Odoo field", mapping_page.text)
        self.assertIn(
            "Choose where each Odoo field gets its value",
            mapping_page.text,
        )
        self.assertIn("Links to other Odoo records", mapping_page.text)
        self.assertIn("such as a product category", mapping_page.text)
        self.assertIn("Find a linked Odoo field", mapping_page.text)
        self.assertIn("data-relation-pagination", mapping_page.text)
        self.assertIn("data-scalar-pagination", mapping_page.text)
        self.assertIn("data-scalar-table-scroll-top", mapping_page.text)
        self.assertIn(
            'aria-label="Scroll scalar target fields horizontally"',
            mapping_page.text,
        )
        self.assertIn("data-scalar-table-scroll", mapping_page.text)
        self.assertIn("Preview", mapping_page.text)
        self.assertIn("Value rules", mapping_page.text)
        self.assertIn("Must be exactly", mapping_page.text)
        self.assertIn("The first characters", mapping_page.text)
        self.assertIn("Plain text (recommended)", mapping_page.text)
        self.assertIn("Advanced: custom pattern", mapping_page.text)
        self.assertIn("Advanced: formula or custom calculation", mapping_page.text)
        self.assertIn("Safe formulas only", mapping_page.text)

        mapping_script = self.client.get("/static/app.js")
        self.assertIn("updateScalarTableScroll", mapping_script.text)
        self.assertIn(
            "new ResizeObserver(updateScalarTableScroll)",
            mapping_script.text,
        )
        self.assertIn('window.addEventListener("beforeunload"', mapping_script.text)
        self.assertIn("scheduleScalarCatalogSearch", mapping_script.text)
        self.assertIn("new AbortController()", mapping_script.text)
        self.assertIn("new DOMParser()", mapping_script.text)
        self.assertIn("window.history.replaceState", mapping_script.text)
        self.assertIn("restoreScalarRow(row)", mapping_script.text)
        self.assertIn("restoreRelationRow(row)", mapping_script.text)
        self.assertIn("scheduleRelationCatalogSearch", mapping_script.text)
        self.assertIn("relationDraftRows", mapping_script.text)
        self.assertNotIn("pendingRedirect", mapping_script.text)
        self.assertNotIn(
            "mappingForm.requestSubmit(saveProgress)",
            mapping_script.text,
        )
        self.assertIn(
            "Searching Odoo fields",
            mapping_script.text,
        )
        self.assertIn(
            'mappingForm.getAttribute("action")',
            mapping_script.text,
        )
        self.assertNotIn("fetch(mappingForm.action", mapping_script.text)
        self.assertIn(
            "Your unsaved changes are still on this page",
            mapping_script.text,
        )
        self.assertIn("hydrateSourceOptions", mapping_script.text)
        self.assertIn("option.defaultSelected = selected", mapping_script.text)
        mapping_styles = self.client.get("/static/app.css")
        self.assertIn(".scalar-table-scroll-top", mapping_styles.text)
        self.assertIn("overflow-x: scroll", mapping_styles.text)
        self.assertIn(".mapping-save-state.unsaved", mapping_styles.text)

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
        mapping_selection = (
            self.app.state.context.repository.get_mapping_source_selection(
                project_id
            )
        )
        self.assertIsNotNone(mapping_selection)
        mapped_customer, product_names, mapped_product = (
            mapping_selection.datasets
        )
        product_name_key, product_name_value = product_names.columns
        self.assertEqual(mapped_customer.dataset_id, customer.dataset_id)
        self.assertEqual(mapped_product.dataset_id, product.dataset_id)
        business_key_id = schema_governance.business_keys[0].key_id
        saved_progress = self.client.post(
            f"/projects/{project_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "save_progress",
                "expected_parent_version": "",
                "expected_working_draft_version": "",
                "target_model_0": "res.partner",
                "mode_0": "upsert",
                "scalar_value_source_0_1": "source",
                "scalar_type_0_1": "string",
                "scalar_case_0_1": "preserve",
                "scalar_formula_0_1": 'coalesce(value, "Unnamed contact")',
                "scalar_compare_0_1": "1",
                "scalar_null_0_1": "distinct",
                "target_model_1": "res.partner",
                "mode_1": "upsert",
                "target_model_2": "res.partner",
                "mode_2": "upsert",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved_progress.status_code, 303)
        saved_progress_page = self.client.get(
            saved_progress.headers["location"]
        )
        self.assertIn("Saved working draft version 1", saved_progress_page.text)
        self.assertIn("No semantic validation was run", saved_progress_page.text)
        self.assertIn("Unsaved checks", saved_progress_page.text)
        self.assertIn("Your saved working draft is loaded", saved_progress_page.text)
        working_draft = (
            self.app.state.context.repository.get_mapping_working_draft(
                project_id
            )
        )
        self.assertEqual(working_draft.version, 1)
        working_by_dataset = {
            item.dataset_id: item
            for item in working_draft.definition.datasets
        }
        self.assertEqual(
            working_by_dataset[customer.dataset_id].fields[0].source_column_key,
            "",
        )
        self.assertIsNone(
            self.app.state.context.repository.get_mapping_revision(project_id)
        )
        submitted = self.client.post(
            f"/projects/{project_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "submit",
                "expected_parent_version": "",
                "expected_working_draft_version": "1",
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
                "scalar_formula_0_1": 'coalesce(value, "Unnamed contact")',
                "scalar_compare_0_1": "1",
                "scalar_null_0_1": "distinct",
                "target_model_1": "res.partner",
                "mode_1": "upsert",
                "source_identity_1": product_name_key.stable_key,
                "business_key_1": business_key_id,
                "identity_source_1_0": product_name_value.stable_key,
                "scalar_value_source_1_1": "source",
                "scalar_source_1_1": product_name_value.stable_key,
                "scalar_type_1_1": "string",
                "scalar_compare_1_1": "1",
                "scalar_null_1_1": "distinct",
                "target_model_2": "res.partner",
                "mode_2": "upsert",
                "source_identity_2": product_code.stable_key,
                "business_key_2": business_key_id,
                "identity_source_2_0": product_code.stable_key,
                "scalar_value_source_2_1": "constant",
                "scalar_literal_2_1": "Imported product",
                "scalar_type_2_1": "string",
                "scalar_case_2_1": "sentence",
                "scalar_search_2_1": "product",
                "scalar_replacement_2_1": "product",
                "scalar_search_mode_2_1": "literal",
                "scalar_replace_all_2_1": "1",
                "scalar_exact_length_2_1": "16",
                "scalar_segment_location_2_1": "first",
                "scalar_segment_length_2_1": "1",
                "scalar_character_class_2_1": "uppercase",
                "scalar_pattern_2_1": "[A-Z][a-z ]{15}",
                "scalar_compare_2_1": "1",
                "scalar_null_2_1": "distinct",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(submitted.status_code, 303)
        submitted_page = self.client.get(submitted.headers["location"])
        self.assertIn("Mapping submitted as version 1", submitted_page.text)
        self.assertIn("Field matches confirmed", submitted_page.text)
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
            revision_by_dataset[customer.dataset_id].fields[0].transform.formula,
            'coalesce(value, "Unnamed contact")',
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
        product_field = revision_by_dataset[product.dataset_id].fields[0]
        self.assertEqual(product_field.transform.case_mode, "sentence")
        self.assertEqual(product_field.transform.search_value, "product")
        self.assertEqual(product_field.validation.exact_length, 16)
        self.assertEqual(product_field.validation.segment_location, "first")
        self.assertEqual(product_field.validation.character_class, "uppercase")

        impact_link = (
            f"/projects/{project_id}/mapping/transformation-impact"
        )
        self.assertIn("Review transformation impact", submitted_page.text)
        impact_page = self.client.get(impact_link)
        self.assertEqual(impact_page.status_code, 200)
        self.assertIn("Transformation impact", impact_page.text)
        self.assertIn("Raw source", impact_page.text)
        self.assertIn("Proposed value", impact_page.text)
        self.assertIn("Download filtered rows (.csv)", impact_page.text)
        self.assertIn("Download all affected rows (.csv)", impact_page.text)
        self.assertIn("Your registered Excel or CSV source remains unchanged", impact_page.text)
        self.assertIn("data-impact-row", impact_page.text)
        impact_csv = self.client.post(
            f"{impact_link}.csv",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(impact_csv.status_code, 200)
        self.assertIn("text/csv", impact_csv.headers["content-type"])
        self.assertIn("Raw source", impact_csv.text)
        self.assertIn("Proposed value", impact_csv.text)
        mapping_script = self.client.get("/static/app.js")
        self.assertIn('new Blob(["\\uFEFF"', mapping_script.text)
        self.assertIn("data-impact-export", mapping_script.text)

        summary = self.client.get(f"/projects/{project_id}/summary")
        self.assertIn("Check all rows", summary.text)
        checked = self.client.post(
            f"/projects/{project_id}/summary/check",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(checked.status_code, 303)
        readiness_page = self.client.get(checked.headers["location"])
        self.assertIn("Ready", readiness_page.text)
        self.assertIn("Review", readiness_page.text)
        self.assertIn("Fix", readiness_page.text)
        self.assertIn("Rows", readiness_page.text)
        self.assertIn("Technical details", readiness_page.text)
        self.assertIn("Create review workbook", readiness_page.text)
        self.assertIn("Odoo remains unchanged", readiness_page.text)
        self.assertIn("prepared rows safely saved", readiness_page.text)
        self.assertIn("data-staging-summary", readiness_page.text)
        self.assertIn("<summary>Technical details</summary>", readiness_page.text)

        readiness = self.app.state.context.readiness
        report = readiness.current_report(project_id)
        assert report is not None
        staging = readiness.current_staging(project_id)
        assert staging is not None
        self.assertEqual(report.staging_run_id, staging.run_id)
        self.assertEqual(report.staging_content_hash, staging.content_hash)
        restored_staging = (
            self.app.state.context.repository.get_canonical_staging_run(
                project_id,
                staging.run_id,
            )
        )
        self.assertIsNotNone(restored_staging)
        self.assertEqual(
            restored_staging.content_hash,
            staging.content_hash,
        )
        sample_row = report.rows[0]
        paged_rows = tuple(
            replace(
                sample_row,
                source_row=index,
                status="blocked" if index <= 120 else "ready",
                identity=f"ROW-{index:04d}",
            )
            for index in range(1, 202)
        )
        paged_datasets = tuple(
            replace(
                item,
                total=201,
                ready=81,
                needs_review=0,
                blocked=120,
            )
            if item.dataset == sample_row.dataset
            else item
            for item in report.datasets
        )
        paged_report = replace(
            report,
            datasets=paged_datasets,
            rows=paged_rows,
        )
        with patch.object(
            readiness,
            "current_report",
            return_value=paged_report,
        ):
            first_page = self.client.get(
                f"/projects/{project_id}/summary"
            )
            self.assertEqual(
                first_page.text.count("data-readiness-row"),
                100,
            )
            self.assertIn("Rows 1-100 of 201", first_page.text)
            self.assertIn("Page 1 of 3", first_page.text)
            self.assertIn("ROW-0100", first_page.text)
            self.assertNotIn("ROW-0101", first_page.text)
            next_match = re.search(
                r'href="([^"]+)" data-readiness-next',
                first_page.text,
            )
            assert next_match is not None
            next_query = parse_qs(
                urlsplit(unescape(next_match.group(1))).query
            )
            self.assertEqual(next_query["page"], ["2"])

            second_page = self.client.get(
                f"/projects/{project_id}/summary?page=2"
            )
            self.assertEqual(
                second_page.text.count("data-readiness-row"),
                100,
            )
            self.assertIn("Rows 101-200 of 201", second_page.text)
            self.assertIn("ROW-0101", second_page.text)
            self.assertIn("ROW-0200", second_page.text)
            self.assertNotIn("ROW-0001", second_page.text)

            clamped_page = self.client.get(
                f"/projects/{project_id}/summary?page=999"
            )
            self.assertEqual(
                clamped_page.text.count("data-readiness-row"),
                1,
            )
            self.assertIn("Rows 201-201 of 201", clamped_page.text)
            self.assertIn("Page 3 of 3", clamped_page.text)
            self.assertIn("ROW-0201", clamped_page.text)

            filtered_page = self.client.get(
                f"/projects/{project_id}/summary",
                params={
                    "status": "blocked",
                    "dataset": sample_row.dataset,
                    "page": "2",
                },
            )
            self.assertEqual(
                filtered_page.text.count("data-readiness-row"),
                20,
            )
            self.assertIn("Rows 101-120 of 120", filtered_page.text)
            self.assertIn("Page 2 of 2", filtered_page.text)
            self.assertNotIn("data-readiness-next", filtered_page.text)
            previous_match = re.search(
                r'href="([^"]+)" data-readiness-previous',
                filtered_page.text,
            )
            assert previous_match is not None
            previous_query = parse_qs(
                urlsplit(unescape(previous_match.group(1))).query
            )
            self.assertEqual(previous_query["status"], ["blocked"])
            self.assertEqual(
                previous_query["dataset"],
                [sample_row.dataset],
            )
            self.assertNotIn("page", previous_query)

        self.assertEqual(len(self.readiness_calls), 1)
        readiness_requests = self.readiness_calls[0][2]
        self.assertEqual(
            [item.model for item in readiness_requests],
            ["res.partner"],
        )
        self.assertEqual(readiness_requests[0].domain[0], "|")
        evidence = self.client.get(
            f"/projects/{project_id}/summary/manifest"
        )
        self.assertEqual(evidence.status_code, 200)
        self.assertIn(
            "application/json",
            evidence.headers["content-type"],
        )
        with patch("impodo.web.app.write_review_workbook") as builder:
            builder.side_effect = lambda _manifest, workbook: Path(
                workbook
            ).write_bytes(b"review package")
            packaged = self.client.post(
                f"/projects/{project_id}/summary/package",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(packaged.status_code, 303)
        packaged_page = self.client.get(packaged.headers["location"])
        self.assertIn("Download review workbook", packaged_page.text)
        workbook = self.client.get(
            f"/projects/{project_id}/summary/workbook"
        )
        self.assertEqual(workbook.status_code, 200)
        self.assertIn(
            "spreadsheetml.sheet",
            workbook.headers["content-type"],
        )

        project = self.app.state.context.repository.get(project_id)
        changed_scope = self.client.post(
            f"/projects/{project_id}/schema",
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
        self.assertIsNone(readiness.current_staging(project_id))
        self.assertIsNotNone(
            self.app.state.context.repository.get_canonical_staging_run(
                project_id,
                staging.run_id,
            )
        )
        self.assertIsNotNone(
            self.app.state.context.repository.get_mapping_working_draft(
                project_id
            )
        )
        stale_mapping = self.client.get(
            f"/projects/{project_id}/mapping"
        )
        self.assertIn(
            "Saved mapping progress belongs to earlier source or schema evidence",
            stale_mapping.text,
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
                "odoo_base_url": "http://127.0.0.1:8069",
                "odoo_database": "odoo19_local",
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
                "odoo_base_url": "https://odoo.example.com",
                "odoo_database": "odoo_review",
                "action": "test",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(remote.status_code, 422)
        self.assertIn(
            "Enter an Odoo API key for this exact remote target",
            remote.text,
        )
        self.assertEqual(len(self.connection_calls), 1)
        self.assertEqual(self.connection_calls[0][1], "local-only-key")

    def test_first_identity_mapping_save_persists_without_validation(self) -> None:
        project_id, dataset, business_key = self._mapping_ready_project(
            scalar_field_count=30
        )
        source_identity = dataset.columns[0]
        context = self.app.state.context

        saved = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["action", "save_progress"],
                    ["expected_parent_version", ""],
                    ["expected_working_draft_version", ""],
                    ["editable_dataset_id", dataset.dataset_id],
                    ["target_model_0", "res.partner"],
                    ["mode_0", "upsert"],
                    ["on_existing_0", "block"],
                    ["source_identity_0", source_identity.stable_key],
                    ["business_key_0", business_key.key_id],
                    [
                        "identity_source_0_0",
                        source_identity.stable_key,
                    ],
                ]
            },
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["message"], "Saved working draft version 1.")
        working = context.repository.get_mapping_working_draft(project_id)
        self.assertIsNotNone(working)
        self.assertEqual(working.version, 1)
        self.assertEqual(
            working.definition.datasets[0].target_identity[0].source_column_keys,
            (source_identity.stable_key,),
        )
        self.assertIsNone(context.repository.get_mapping_revision(project_id))

    def test_selection_choices_load_and_save_from_the_mapping_dialog(self) -> None:
        project_id, dataset, business_key = self._mapping_ready_project(
            scalar_field_count=1,
            selection_field=True,
        )
        source_identity, source_value = dataset.columns

        page = self.client.get(f"/projects/{project_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-value-match-dialog", page.text)
        self.assertIn("Match values", page.text)
        with patch(
            "impodo.web.app._source_value_choices",
            return_value=(
                {"value": "French", "count": 12},
                {"value": "German", "count": 4},
            ),
        ):
            choices = self.client.post(
                f"/projects/{project_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "scalar",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_value.stable_key,
                    "target_model": "res.partner",
                    "target_field": "field_0000",
                    "business_key_id": "",
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(choices.status_code, 200)
        self.assertEqual(
            choices.json()["target_choices"],
            [
                {"value": "fr_FR", "label": "French (France)"},
                {"value": "de_DE", "label": "German"},
            ],
        )
        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "source"],
            ["scalar_source_0_1", source_value.stable_key],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
            [
                "scalar_value_matches_0_1",
                '[{"source_value":"French","target_value":"fr_FR"}]',
            ],
        ]
        saved = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        working = self.app.state.context.repository.get_mapping_working_draft(
            project_id
        )
        self.assertEqual(
            working.definition.datasets[0].fields[0].value_mappings,
            (ValueMapping("French", "fr_FR"),),
        )

    def test_relationship_choices_are_read_once_without_exposing_odoo_ids(
        self,
    ) -> None:
        project_id, dataset, business_key = self._mapping_ready_project(
            scalar_field_count=0,
            relationship_field_count=1,
        )
        source_value = dataset.columns[1]
        context = self.app.state.context
        calls = []

        def readiness_reader(project, metadata_requests, record_requests):
            calls.append((metadata_requests, record_requests))
            metadata = _browser_schema(project)
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "res.partner": (
                        TargetRecord("res.partner", 10, {"ref": "FR"}),
                        TargetRecord("res.partner", 11, {"ref": "DE"}),
                        TargetRecord("res.partner", 12, {"ref": "BE"}),
                        TargetRecord("res.partner", 13, {"ref": "BE"}),
                    )
                },
                requested_fields={"res.partner": ("ref",)},
            )

        context.readiness_reader = readiness_reader
        with patch(
            "impodo.web.app._source_value_choices",
            return_value=({"value": "FRA", "count": 3},),
        ):
            response = self.client.post(
                f"/projects/{project_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "relationship",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_value.stable_key,
                    "target_model": "res.partner",
                    "target_field": "relation_0000",
                    "business_key_id": business_key.key_id,
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]), 1)
        self.assertEqual(
            response.json()["target_choices"],
            [
                {"value": "DE", "label": "DE"},
                {"value": "FR", "label": "FR"},
            ],
        )
        self.assertEqual(response.json()["ambiguous_values"], ["BE"])
        self.assertNotIn("odoo_id", response.text)
        source_identity = dataset.columns[0]
        saved = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["action", "save_progress"],
                    ["expected_parent_version", ""],
                    ["expected_working_draft_version", ""],
                    ["editable_dataset_id", dataset.dataset_id],
                    ["target_model_0", "res.partner"],
                    ["mode_0", "upsert"],
                    ["on_existing_0", "block"],
                    ["source_identity_0", source_identity.stable_key],
                    ["business_key_0", business_key.key_id],
                    ["identity_source_0_0", source_identity.stable_key],
                    ["visible_relation_target_0", "relation_0000"],
                    ["relation_source_0_0", source_value.stable_key],
                    ["relation_origin_0_0", "target_catalog"],
                    ["relation_key_0_0", business_key.key_id],
                    ["relation_operation_0_0", "replace"],
                    ["relation_compare_0_0", "1"],
                    ["relation_missing_0_0", "error"],
                    ["relation_ambiguous_0_0", "error"],
                    ["relation_null_0_0", "distinct"],
                    ["relation_separator_0_0", ";"],
                    [
                        "relation_value_matches_0_0",
                        '[{"source_value":"FRA","target_value":"FR"}]',
                    ],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        working = context.repository.get_mapping_working_draft(project_id)
        self.assertEqual(
            working.definition.datasets[0]
            .relationships[0]
            .resolver.value_mappings,
            (ValueMapping("FRA", "FR"),),
        )

    def test_country_matching_uses_reviewed_code_without_schema_recapture(
        self,
    ) -> None:
        project_id, dataset, business_key = self._mapping_ready_project(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model="res.country",
        )
        source_identity, source_country = dataset.columns
        context = self.app.state.context
        calls = []

        def readiness_reader(project, metadata_requests, record_requests):
            calls.append((metadata_requests, record_requests))
            metadata = _browser_schema(project)
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "res.country": (
                        TargetRecord(
                            "res.country",
                            1,
                            {"code": "FR", "name": "France"},
                        ),
                        TargetRecord(
                            "res.country",
                            2,
                            {"code": "BE", "name": "Belgium"},
                        ),
                    )
                },
                requested_fields={"res.country": ("code", "name")},
            )

        context.readiness_reader = readiness_reader
        page = self.client.get(f"/projects/{project_id}/mapping")

        self.assertEqual(page.status_code, 200)
        schema = context.repository.get_odoo_schema_catalog(project_id)
        self.assertNotEqual(
            schema.target_hash,
            target_identity_hash(
                connection_mode="LOCAL",
                base_url="http://127.0.0.1:8069",
                database="odoo19_local",
            ),
        )
        self.assertIn("Country code — recommended", page.text)
        self.assertIn(
            'value="odoo-standard:res.country:code" selected',
            page.text,
        )
        with patch(
            "impodo.web.app._source_value_choices",
            return_value=({"value": "FRA", "count": 3},),
        ):
            choices = self.client.post(
                f"/projects/{project_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "relationship",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_country.stable_key,
                    "target_model": "res.partner",
                    "target_field": "relation_0000",
                    "business_key_id": "odoo-standard:res.country:code",
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(choices.status_code, 200)
        self.assertEqual(
            choices.json()["target_choices"],
            [
                {"value": "BE", "label": "Belgium (BE)"},
                {"value": "FR", "label": "France (FR)"},
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1][0].model, "res.country")
        self.assertEqual(calls[0][1][0].fields, ("code", "name"))
        self.assertNotIn("odoo_id", choices.text)

        saved = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["action", "save_progress"],
                    ["expected_parent_version", ""],
                    ["expected_working_draft_version", ""],
                    ["editable_dataset_id", dataset.dataset_id],
                    ["target_model_0", "res.partner"],
                    ["mode_0", "upsert"],
                    ["on_existing_0", "block"],
                    ["source_identity_0", source_identity.stable_key],
                    ["business_key_0", business_key.key_id],
                    ["identity_source_0_0", source_identity.stable_key],
                    ["visible_relation_target_0", "relation_0000"],
                    ["relation_source_0_0", source_country.stable_key],
                    ["relation_origin_0_0", "target_catalog"],
                    [
                        "relation_key_0_0",
                        "odoo-standard:res.country:code",
                    ],
                    ["relation_operation_0_0", "replace"],
                    ["relation_compare_0_0", "1"],
                    ["relation_missing_0_0", "error"],
                    ["relation_ambiguous_0_0", "error"],
                    ["relation_null_0_0", "distinct"],
                    ["relation_separator_0_0", ";"],
                    [
                        "relation_value_matches_0_0",
                        '[{"source_value":"FRA","target_value":"FR"}]',
                    ],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        working = context.repository.get_mapping_working_draft(project_id)
        resolver = working.definition.datasets[0].relationships[0].resolver
        self.assertEqual(
            resolver.key_mappings,
            (ReferenceKeyMapping(source_country.stable_key, "code"),),
        )
        self.assertEqual(
            resolver.value_mappings,
            (ValueMapping("FRA", "FR"),),
        )

    def test_reviewed_reference_matching_is_not_country_specific(self) -> None:
        for related_model, label in (
            ("res.lang", "Language code — recommended"),
            ("res.currency", "Currency code — recommended"),
        ):
            with self.subTest(related_model=related_model):
                project_id, _dataset, _business_key = self._mapping_ready_project(
                    scalar_field_count=0,
                    relationship_field_count=1,
                    relationship_model=related_model,
                )

                page = self.client.get(f"/projects/{project_id}/mapping")

                self.assertEqual(page.status_code, 200)
                self.assertIn(label, page.text)
                self.assertNotIn("No matching rule available", page.text)

    def test_relationship_without_matching_rule_has_clear_disabled_state(
        self,
    ) -> None:
        project_id, _dataset, _business_key = self._mapping_ready_project(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model="res.company",
        )

        page = self.client.get(f"/projects/{project_id}/mapping")

        self.assertEqual(page.status_code, 200)
        self.assertIn("No matching rule available", page.text)
        self.assertIn(
            "Choose a matching rule before matching values.",
            page.text,
        )
        self.assertIn('name="relation_key_0_0" disabled', page.text)

    def test_relationship_catalog_is_searchable_and_progressively_disclosed(
        self,
    ) -> None:
        project_id, dataset, _business_key = self._mapping_ready_project(
            scalar_field_count=1,
            relationship_field_count=51,
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        initial = context.mapping_workspace.save_working_draft(
            project_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    relationships=(
                        RelationshipMapping(
                            target_field="relation_0050",
                            kind="many2one",
                            source_column_keys=(source_value.stable_key,),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.TARGET_CATALOG,
                                model="res.partner",
                                key_mappings=(
                                    ReferenceKeyMapping(
                                        source_column_key=source_value.stable_key,
                                        target_field="ref",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            expected_version=None,
            actor=context.actor,
        )
        self.assertEqual(initial.version, 1)

        page = self.client.get(f"/projects/{project_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("data-relation-field-row"), 3)
        self.assertIn("Showing 3 of 51 linked fields", page.text)
        self.assertIn('data-relation-page-size="3"', page.text)
        self.assertLess(
            page.text.index("relation_0050"),
            page.text.index("relation_0000"),
        )
        self.assertNotIn("relation_0002</code>", page.text)

        expanded = self.client.get(
            f"/projects/{project_id}/mapping?relation_page_size=20"
        )
        self.assertEqual(expanded.text.count("data-relation-field-row"), 20)
        self.assertIn('data-relation-page-size="20"', expanded.text)

        searched = self.client.get(
            f"/projects/{project_id}/mapping?relation_query=relation_0049"
        )
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.text.count("data-relation-field-row"), 1)
        self.assertIn("Linked Field 0049", searched.text)
        self.assertIn("Showing 1 of 1 linked fields", searched.text)

        searched_by_model = self.client.get(
            f"/projects/{project_id}/mapping?relation_query=res.partner"
        )
        self.assertEqual(
            searched_by_model.text.count("data-relation-field-row"),
            3,
        )
        self.assertIn("Showing 3 of 51 linked fields", searched_by_model.text)

        last_page = self.client.get(
            f"/projects/{project_id}/mapping?relation_page=17"
        )
        self.assertEqual(last_page.text.count("data-relation-field-row"), 3)
        self.assertIn("relation_0049", last_page.text)

        rejected_size = self.client.get(
            f"/projects/{project_id}/mapping?relation_page_size=100"
        )
        self.assertEqual(
            rejected_size.text.count("data-relation-field-row"),
            3,
        )
        self.assertIn('data-relation-page-size="3"', rejected_size.text)

    def test_large_mapping_catalog_is_paged_and_saved_sparsely(self) -> None:
        project_id, dataset, business_key = self._mapping_ready_project(
            scalar_field_count=1500
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        initial = context.mapping_workspace.save_working_draft(
            project_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=tuple(
                        ScalarFieldMapping(
                            target_field=f"field_{index:04d}",
                            source_column_key=source_value.stable_key,
                            value_source=ScalarValueSource.SOURCE,
                        )
                        for index in range(1500)
                    ),
                ),
            ),
            expected_version=None,
            actor=context.actor,
        )
        self.assertEqual(initial.version, 1)

        page = self.client.get(f"/projects/{project_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("data-scalar-mapping-row"), 3)
        self.assertIn("Showing 3 of 1500 fields", page.text)
        self.assertIn("Page 1 of 500", page.text)
        self.assertIn("field_0000", page.text)
        self.assertNotIn("field_0003</code>", page.text)
        self.assertIn("template data-source-column-options", page.text)
        self.assertLess(page.text.count(source_value.stable_key), 100)

        last_page = self.client.get(
            f"/projects/{project_id}/mapping?scalar_page=500"
        )
        self.assertEqual(last_page.text.count("data-scalar-mapping-row"), 3)
        self.assertIn("field_1499", last_page.text)
        self.assertIn("scalar_page=500", last_page.text)

        expanded = self.client.get(
            f"/projects/{project_id}/mapping?scalar_page_size=50"
        )
        self.assertEqual(expanded.text.count("data-scalar-mapping-row"), 50)
        self.assertIn("field_0049", expanded.text)
        self.assertNotIn("field_0050</code>", expanded.text)
        self.assertIn('data-scalar-page-size="50"', expanded.text)

        rejected_size = self.client.get(
            f"/projects/{project_id}/mapping?scalar_page_size=100"
        )
        self.assertEqual(
            rejected_size.text.count("data-scalar-mapping-row"),
            3,
        )
        self.assertIn('data-scalar-page-size="3"', rejected_size.text)

        searched = self.client.get(
            f"/projects/{project_id}/mapping?field_query=field_1499"
        )
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.text.count("data-scalar-mapping-row"), 1)
        self.assertIn("field_1499", searched.text)
        self.assertNotIn("field_0000</code>", searched.text)

        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", "1"],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "constant"],
            ["scalar_literal_0_1", "Updated safely"],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
        ]
        saved = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={"entries": entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn("redirect_url", saved.json())
        working = context.repository.get_mapping_working_draft(project_id)
        self.assertEqual(working.version, 2)
        self.assertEqual(len(working.definition.datasets[0].fields), 1500)
        updated = {
            item.target_field: item
            for item in working.definition.datasets[0].fields
        }
        self.assertEqual(updated["field_0000"].literal_value, "Updated safely")
        self.assertEqual(
            updated["field_1499"].source_column_key,
            source_value.stable_key,
        )

        denied = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={"entries": entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": "incorrect",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            context.repository.get_mapping_working_draft(project_id).version,
            2,
        )

        invalid_entries = [
            list(item)
            for item in entries
            if item[0] not in {"source_identity_0", "identity_source_0_0"}
        ]
        for item in invalid_entries:
            if item[0] == "action":
                item[1] = "submit"
            elif item[0] == "expected_working_draft_version":
                item[1] = "2"
        invalid = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={"entries": invalid_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["expected_working_draft_version"], 3)
        self.assertEqual(invalid.json()["expected_parent_version"], 1)

        retry_entries = [list(item) for item in entries]
        for item in retry_entries:
            if item[0] == "expected_working_draft_version":
                item[1] = "3"
            elif item[0] == "expected_parent_version":
                item[1] = "1"
        retried = self.client.post(
            f"/projects/{project_id}/mapping/save",
            json={"entries": retry_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(
            context.repository.get_mapping_working_draft(project_id).version,
            4,
        )

        oversized = b'{"entries":[["csrf_token","' + (
            b"x" * (5 * 1024 * 1024)
        ) + b'"]]}'
        rejected = self.client.post(
            f"/projects/{project_id}/mapping/save",
            content=oversized,
            headers={
                **POST_HEADERS,
                "Content-Type": "application/json",
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(rejected.status_code, 413)
        self.assertEqual(
            context.repository.get_mapping_working_draft(project_id).version,
            4,
        )

        excessive_form = "&".join(
            [f"field_{index}=x" for index in range(25_001)]
        )
        recovered = self.client.post(
            f"/projects/{project_id}/mapping/save",
            content=excessive_form,
            headers={
                **POST_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        self.assertEqual(recovered.status_code, 303)
        self.assertIn("save_error=request_rejected", recovered.headers["location"])
        recovery_page = self.client.get(recovered.headers["location"])
        self.assertIn("No mapping change was saved", recovery_page.text)

    def test_saved_prepared_data_has_plain_recovery_ui(self) -> None:
        project_id, _dataset, _business_key = self._mapping_ready_project(
            scalar_field_count=0,
        )
        context = self.app.state.context
        staging = MagicMock(
            total_rows=12,
            mapping_version=3,
            run_id="f0cd6d32-80d9-4e31-9bcb-d316d83cf0b8",
            content_hash="sha256:" + "7" * 64,
            datasets=(),
        )

        with (
            patch.object(
                context.readiness,
                "current_staging",
                return_value=staging,
            ),
            patch.object(
                context.readiness,
                "current_report",
                return_value=None,
            ),
        ):
            page = self.client.get(f"/projects/{project_id}/summary")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Your prepared data is safe", page.text)
        self.assertIn("12 prepared rows", page.text)
        self.assertIn("Try Odoo comparison again", page.text)
        self.assertIn("Prepared data is stored locally", page.text)
        self.assertIn("<details", page.text)
        self.assertIn("<summary>Technical details</summary>", page.text)
        self.assertNotIn("<details open", page.text)
        self.assertNotIn("canonical_staging", page.text)

    def _mapping_ready_project(
        self,
        *,
        scalar_field_count: int,
        relationship_field_count: int = 0,
        relationship_model: str = "res.partner",
        selection_field: bool = False,
    ):
        context = self.app.state.context
        created = context.projects.create_project(
            actor=context.actor,
            name="Large mapping",
            source_system="CSV",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            created,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
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
        dataset = SourceDataset(
            dataset_id="dataset:large",
            name="large_contacts",
            file_id="source:large",
            table_key="contacts",
            source_sha256="sha256:" + "1" * 64,
            catalog_hash="sha256:" + "2" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
            row_count=1,
            columns=(
                SourceDatasetColumn(1, "Reference", "column:ref", "string"),
                SourceDatasetColumn(2, "Value", "column:value", "string"),
            ),
        )
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=registered.project_id,
            created_at=now,
            created_by=context.actor.identity.display_name,
            datasets=(dataset,),
            content_hash="sha256:" + "3" * 64,
        )
        context.repository.save_source_selection(
            registered.project_id,
            selection,
            actor=context.actor,
        )
        fields = (
            SchemaField(
                name="ref",
                label="Reference",
                type="char",
                required=True,
                readonly=False,
                relation=None,
                relation_field=None,
                selection=(),
            ),
            *(
                SchemaField(
                    name=f"field_{index:04d}",
                    label=f"Field {index:04d}",
                    type=(
                        "selection"
                        if selection_field and index == 0
                        else "char"
                    ),
                    required=False,
                    readonly=False,
                    relation=None,
                    relation_field=None,
                    selection=(
                        (
                            ("fr_FR", "French (France)"),
                            ("de_DE", "German"),
                        )
                        if selection_field and index == 0
                        else ()
                    ),
                )
                for index in range(scalar_field_count)
            ),
            *(
                SchemaField(
                    name=f"relation_{index:04d}",
                    label=f"Linked Field {index:04d}",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation=relationship_model,
                    relation_field=None,
                    selection=(),
                )
                for index in range(relationship_field_count)
            ),
        )
        schema = OdooSchemaCatalog(
            project_id=registered.project_id,
            # A schema bundle hash includes its selected model scope and is
            # intentionally different from the live target fingerprint.
            target_hash="sha256:" + "5" * 64,
            captured_at=now,
            captured_by=context.actor.identity.display_name,
            connection_mode=registered.odoo_connection_mode.value,
            database=registered.odoo_database,
            odoo_version="19.0",
            models=(SchemaModel("res.partner", "Contact", fields),),
            content_hash="sha256:" + "4" * 64,
        )
        context.repository.save_odoo_schema_catalog(
            registered.project_id,
            schema,
            actor=context.actor,
        )
        business_key = BusinessKeyDefinition(
            key_id="res.partner:ref",
            model="res.partner",
            key_fields=("ref",),
            description="Unique reference",
            status=BusinessKeyStatus.CONFIRMED,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            project_id=registered.project_id,
            catalog_hash=schema.content_hash,
            permitted_models=("res.partner",),
            business_keys=(business_key,),
            recorded_at=now,
            recorded_by=context.actor.identity.display_name,
        )
        context.repository.save_schema_governance(
            registered.project_id,
            governance,
            actor=context.actor,
        )
        return registered.project_id, dataset, business_key

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


def _browser_schema(project) -> MetadataSnapshot:
    return MetadataSnapshot(
        fingerprint=TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode=project.odoo_connection_mode.value,
                base_url=project.odoo_base_url,
                database=project.odoo_database,
            ),
            connection_mode=project.odoo_connection_mode.value,
            database=project.odoo_database,
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


def _browser_model_catalog(project) -> RecordSnapshot:
    fingerprint = TargetFingerprint(
        target_hash=target_identity_hash(
            connection_mode=project.odoo_connection_mode.value,
            base_url=project.odoo_base_url,
            database=project.odoo_database,
        ),
        connection_mode=project.odoo_connection_mode.value,
        database=project.odoo_database,
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
