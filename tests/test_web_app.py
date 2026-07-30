from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest

from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.worksheet.table import Table

from impodo.access import Actor, ActorIdentity, Capability
from impodo.connectors import MetadataSnapshot
from impodo.local_stack import (
    LocalStackCheck,
    LocalStackService,
    LocalStackStatus,
    ReadinessLevel,
)
from impodo.models import (
    EnvironmentFingerprint,
    FieldMetadata,
    ModelMetadata,
)
from impodo.projects import OdooConnectionMode, ProjectStatus
from impodo.secrets import MemorySecretStore
from impodo.web import create_app


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
        self.assertIn("Impodo - Import Anything into Odoo", projects.text)
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
        self.config = Path(self.temporary.name) / "local-odoo.conf"
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
        self.picker_calls = 0

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

        local_stack = LocalStackService(
            config_picker=pick_config,
            probe=probe,
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

        project = self.app.state.context.repository.get(self.project_id)
        self.assertEqual(project.odoo_base_url, "")
        self.assertEqual(project.odoo_database, "")
        config_bytes = str(self.config).encode()
        for path in self.app.state.context.repository.project_directory(
            self.project_id
        ).rglob("*"):
            if path.is_file():
                self.assertNotIn(config_bytes, path.read_bytes())

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


class PhaseAWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.secrets = MemorySecretStore()
        self.connection_calls: list[tuple[str, str, OdooConnectionMode | None]] = []
        self.schema_calls: list[tuple[str, str]] = []

        def connection_tester(project, api_key):
            self.connection_calls.append(
                (project.project_id, api_key, project.odoo_connection_mode)
            )
            return "Read-only local connection succeeded: DEV / Odoo 19.4"

        def schema_reader(project, api_key):
            self.schema_calls.append((project.project_id, api_key))
            return _browser_schema()

        self.app = create_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=self.secrets,
            connection_tester=connection_tester,
            schema_reader=schema_reader,
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

        phase_b = self.client.get(f"/projects/{project_id}/sources")
        self.assertEqual(phase_b.status_code, 200)
        self.assertIn("No source catalog yet", phase_b.text)
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
        self.assertIn("Map source columns to Odoo", mapping_page.text)
        self.assertIn("res.partner::name", mapping_page.text)
        self.assertIn("Existing Odoo catalog", mapping_page.text)
        self.assertIn("inverse parent_id", mapping_page.text)

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
                "scalar_source_0_1": customer_name.stable_key,
                "scalar_type_0_1": "string",
                "scalar_compare_0_1": "1",
                "scalar_null_0_1": "distinct",
                "target_model_1": "res.partner",
                "mode_1": "upsert",
                "source_identity_1": product_code.stable_key,
                "business_key_1": business_key_id,
                "identity_source_1_0": product_code.stable_key,
                "scalar_source_1_1": product_name.stable_key,
                "scalar_type_1_1": "string",
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
                description="Contact",
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
