"""Explicit browser fixtures and builders shared by focused web tests."""

from __future__ import annotations

from dataclasses import asdict, replace

from contextlib import contextmanager

from datetime import date, datetime, timedelta, timezone

from html import unescape

from io import BytesIO

import json

import multiprocessing

from pathlib import Path

import re

import tempfile

import time

from types import SimpleNamespace

import unittest

from unittest.mock import MagicMock, patch

from urllib.parse import parse_qs, urlsplit

from uuid import uuid4

import duckdb

from fastapi.testclient import TestClient

from openpyxl import Workbook

from openpyxl.worksheet.table import Table

from impodo.domain.shared.access import Actor, ActorIdentity, Capability

from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTransportError,
    MetadataSnapshot,
    RecordSnapshot,
)

from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader

from impodo.adapters.odoo.local_stack import (
    LocalStackCheck,
    LocalStackService,
    LocalStackStartResult,
    LocalStackStatus,
    ReadinessLevel,
)

from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldHandling,
    ValueMapping,
)

from impodo.domain.mapping.validation.evidence import (
    MappingValidationStatus,
    mapping_issue_fingerprint,
)

from impodo.domain.source_binding import FileSourceBinding

from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)

from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageDataset,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
    source_column_contract_hash,
)

from impodo.application.data_version.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)

from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus

from impodo.domain.odoo_source_capture import (
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureValueColumn,
)

from impodo.domain.odoo_comparison import OdooComparisonOutcome

from impodo.application.preflight_service import MANIFEST_NAME

from impodo.application.odoo_connection_service import OdooConnectionTestService

from impodo.application.workspace.execution.load_jobs import LoadJobResult

from impodo.application.odoo_read_failures import (
    OdooReadCredentialMissingError,
    OdooReadFailureCode,
    OdooReadWorkflowError,
)

from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    OdooWriteIdentity,
    ProtectedOdooReadContext,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)

from impodo.domain.shared.models import canonical_json_text

from impodo.domain.execution.odoo_readback import ReadbackRecord

from impodo.domain.workspace.workbench import OdooConnectionMode, WorkspaceStatus, SourceMode

from impodo.application.workspace.preparation.job_models import PreparationJobStatus, PreparationWorkspace

from impodo.domain.execution.planner import PreflightRequirementPlan

from impodo.domain.preparation.quality import (
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRuleFamily,
)

from impodo.domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
)

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore

from impodo.application.shared.secrets import SecretStoreError

from impodo.domain.preparation.staging_contracts import CanonicalControlTotal

from impodo.web.app import create_local_app

from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)

from impodo.web.composition.target_readers import _source_value_choices

from impodo.domain.workspace.errors import WorkspaceError

from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH

ROOT = Path(__file__).resolve().parents[2]

POST_HEADERS = {
    "Origin": "http://testserver",
    "Sec-Fetch-Site": "same-origin",
}

def _source_column_profile(
    ordinal: int,
    name: str,
    value: str,
) -> SourceColumnProfile:
    return SourceColumnProfile(
        ordinal=ordinal,
        name=name,
        candidate_type="string",
        null_count=0,
        non_null_count=1,
        distinct_count=1,
        distinct_count_is_exact=True,
        duplicate_count=0,
        minimum=value,
        maximum=value,
        minimum_length=len(value),
        maximum_length=len(value),
    )

def _replace_run_target_setup(
    context,
    workspace_id: str,
    *,
    connection_mode: OdooConnectionMode,
    base_url: str,
    database: str,
    intended_applications: tuple[str, ...] = (),
) -> None:
    workspace = context.migration_workspaces.get(
        workspace_id,
        actor=context.actor,
    )
    current = context.migration_run_target_setup.get(
        workspace.migration_run_id,
        actor=context.actor,
    )
    context.migration_run_target_setup.replace(
        workspace.migration_run_id,
        actor=context.actor,
        expected_revision=current.revision if current is not None else None,
        connection_mode=connection_mode.value,
        base_url=base_url,
        database=database,
        intended_applications=intended_applications,
    )

def _hold_duckdb_files(
    paths: tuple[str, ...],
    ready,
    release,
    status,
) -> None:
    """Own real cross-process DuckDB locks until the test releases them."""

    connections = []
    try:
        connections = [duckdb.connect(path) for path in paths]
        status.put(("ready", ""))
        ready.set()
        release.wait(60)
    except Exception as error:
        status.put(("error", f"{type(error).__name__}: {error}"))
        ready.set()
    finally:
        for connection in reversed(connections):
            connection.close()

@contextmanager
def _spawned_duckdb_locks(*paths: Path):
    """Hold database files from another process using DuckDB's real locks."""

    process_context = multiprocessing.get_context("spawn")
    ready = process_context.Event()
    release = process_context.Event()
    status = process_context.Queue()
    process = process_context.Process(
        target=_hold_duckdb_files,
        args=(tuple(str(path) for path in paths), ready, release, status),
        name="impodo-test-duckdb-lock-holder",
    )
    process.start()
    try:
        if not ready.wait(10):
            raise AssertionError("DuckDB lock holder did not start")
        state, message = status.get(timeout=2)
        if state != "ready":
            raise AssertionError(f"DuckDB lock holder failed: {message}")
        yield
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        status.close()

def _wait_for_preparation(
    client: TestClient,
    progress_url: str,
    *,
    timeout: float = 20.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    status_url = f"{progress_url}/status"
    while time.monotonic() < deadline:
        response = client.get(status_url)
        if response.status_code == 200:
            payload = response.json()
            if payload["status"] not in {"QUEUED", "RUNNING"}:
                return payload
        time.sleep(0.05)
    raise AssertionError("background preparation did not finish in time")

def _wait_for_odoo_capture(
    client: TestClient,
    progress_url: str,
    *,
    timeout: float = 5.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"{progress_url}/status")
        if response.status_code == 200:
            payload = response.json()
            if payload["status"] not in {"QUEUED", "RUNNING"}:
                return payload
        time.sleep(0.02)
    raise AssertionError("background Odoo capture did not finish in time")

def _wait_for_load(
    client: TestClient,
    progress_url: str,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"{progress_url}/status")
        if response.status_code == 200:
            payload = response.json()
            if payload["status"] not in {"QUEUED", "RUNNING"}:
                return payload
        time.sleep(0.02)
    raise AssertionError("background Odoo load did not finish in time")

def _created_workspace_id(app, response) -> str:
    parts = urlsplit(response.headers["location"]).path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "workspaces":
        return parts[1]
    if len(parts) < 2 or parts[0] != "projects":
        raise AssertionError("Project creation returned an unexpected location")
    workspaces = app.state.context.migration_workspaces.list_for_project(
        parts[1],
        actor=app.state.context.actor,
    )
    if len(workspaces) != 1:
        raise AssertionError("New Project did not create one authoring workspace")
    return workspaces[0].workspace_id

def _workspace_data_version_id(context, workspace_id: str) -> str:
    return context.migration_workspaces.get(
        workspace_id,
        actor=context.actor,
    ).data_version_id

class ProjectWorkspaceBuilder:
    """Create one workspace with explicit Project-source ownership inputs."""

    def __init__(self, context) -> None:
        self._context = context

    def create(
        self,
        *,
        name: str,
        source_system: str,
        source_mode: str = "FILE",
    ):
        return self._context.project_authoring.create(
            actor=self._context.actor,
            display_name=name,
            source_mode=source_mode,
            creation_request_id=str(uuid4()),
            source_system_identity=source_system,
        ).workspace_state

def _csrf(html: str) -> str:
    matched = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if matched is None:
        raise AssertionError("CSRF token not found")
    return matched.group(1)

def _browser_schema(workspace_state) -> MetadataSnapshot:
    return MetadataSnapshot(
        fingerprint=TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode=workspace_state.odoo_connection_mode.value,
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
            ),
            connection_mode=workspace_state.odoo_connection_mode.value,
            database=workspace_state.odoo_database,
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
                        stored=True,
                        computed=False,
                        has_inverse=False,
                        related=False,
                        translated=False,
                        company_dependent=False,
                        searchable=True,
                        sortable=True,
                        exportable=True,
                    ),
                    "write_date": FieldMetadata(
                        name="write_date",
                        type="datetime",
                        label="Last Updated on",
                        readonly=True,
                        stored=True,
                        computed=False,
                        has_inverse=False,
                        related=False,
                        translated=False,
                        company_dependent=False,
                        searchable=True,
                        sortable=True,
                        exportable=True,
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

class _BrowserOdooCaptureGateway:
    def __init__(self, workspace_state, schema: OdooSchemaCatalog) -> None:
        self.workspace_state = workspace_state
        self.schema = schema
        self.now = datetime.now(timezone.utc)
        self.calls: list[str] = []
        self.context = ProtectedOdooReadContext(
            language="en_US",
            timezone="UTC",
            primary_company_id=1,
            allowed_company_ids=(1,),
        )

    def probe_identity(self, request, *, cancellation=None):
        self.calls.append("identity")
        return (
            OdooReadIdentity(
                target_hash=self.schema.connection_target_hash,
                principal_hash=self.schema.read_principal_hash,
                permission_hash=self.schema.read_permission_hash,
                context_hash=self.schema.read_context_hash,
                readable_models=request.schema_model_names,
                observed_at=self.now.isoformat(),
            ),
            self.context,
        )

    def probe_schema(self, request, context, *, cancellation=None):
        self.calls.append("schema")
        return _browser_schema(self.workspace_state)

    def open_capture(self, request, context, *, cancellation=None):
        self.calls.append("open")
        return _BrowserOdooCaptureSession(request, self.now)

    def sample(self, request, context, *, limit, cancellation=None):
        raise AssertionError("Freeze action does not run a sample")

class _BrowserOdooCaptureSession:
    def __init__(self, request, now: datetime) -> None:
        self._page = OdooCapturePage(
            first_row_ordinal=1,
            odoo_ids=(11, 12),
            write_dates=(now, now + timedelta(seconds=1)),
            columns=(
                OdooCaptureValueColumn(
                    field_name="name",
                    field_type="char",
                    values=("Alice", "Bob"),
                ),
            ),
            response_bytes=100,
            normalized_bytes=20,
        )
        self._accounting = OdooCaptureAccounting(
            high_water_id=12,
            row_count=2,
            page_count=1,
            record_request_count=2,
            response_bytes=102,
            normalized_bytes=20,
            capture_started_at=now,
            capture_finished_at=now + timedelta(seconds=2),
            consistency=request.consistency,
            target_instance_assurance=request.target_instance_assurance,
            consistency_limitation=(
                "Native pages are not one database-wide point-in-time snapshot."
            ),
        )

    def pages(self):
        return iter((self._page,))

    @property
    def accounting(self):
        return self._accounting

def _browser_model_catalog(workspace_state) -> RecordSnapshot:
    fingerprint = TargetFingerprint(
        target_hash=target_identity_hash(
            connection_mode=workspace_state.odoo_connection_mode.value,
            base_url=workspace_state.odoo_base_url,
            database=workspace_state.odoo_database,
        ),
        connection_mode=workspace_state.odoo_connection_mode.value,
        database=workspace_state.odoo_database,
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

class LocalBrowserSecurityTestCase(unittest.TestCase):
    """Create isolated state for LocalBrowserSecurityTests capability tests."""

    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.project_root = Path(self.temporary.name) / "impodo-data"
        self.app = create_local_app(
            self.project_root,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=MemorySecretStore(),
        )
        self.workspaces = ProjectWorkspaceBuilder(self.app.state.context)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

class LocalStackBrowserTestCase(unittest.TestCase):
    """Create isolated state for LocalStackBrowserTests capability tests."""

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
            lambda workspace_state, _profile: _browser_schema(workspace_state).fingerprint
        )
        self.project_root = Path(self.temporary.name) / "impodo-data"
        self.app = create_local_app(
            self.project_root,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=MemorySecretStore(),
            local_stack_service=local_stack,
            local_odoo_reader=self.local_odoo_reader,
        )
        self.workspaces = ProjectWorkspaceBuilder(self.app.state.context)
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
                "creation_request_id": str(uuid4()),
                "display_name": "Local readiness",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.workspace_id = _created_workspace_id(self.app, created)
        context = self.app.state.context
        workspace_state = context.queries.get(self.workspace_id)
        context.intake.accept(
            self.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            display_name="local-readiness.csv",
            stream=BytesIO(b"code,name\nP001,Example\n"),
        )
        registered = context.workspace_states.register(
            self.workspace_id,
            actor=context.actor,
            expected_revision=context.queries.get(self.workspace_id).revision,
        )
        self.workspace_revision = registered.revision

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _select_and_start_stack(self) -> None:
        selected = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(selected.status_code, 200)
        started = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/start",
            data={
                "csrf_token": self.csrf,
                "confirm_start": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(started.status_code, 303)

    def _register_local_project(self, *, database: str = "odoo19_local"):
        context = self.app.state.context
        workspace_state = context.workspace_states.repository.get(self.workspace_id)
        now = datetime.now(timezone.utc)
        registered = replace(
            workspace_state,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:18069",
            odoo_database=database,
            status=WorkspaceStatus.REGISTERED,
            revision=workspace_state.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            registered,
            expected_revision=workspace_state.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        _replace_run_target_setup(
            context,
            self.workspace_id,
            connection_mode=OdooConnectionMode.LOCAL,
            base_url="http://127.0.0.1:18069",
            database=database,
        )
        return registered

class ProjectSetupBrowserTestCase(unittest.TestCase):
    """Create isolated state for ProjectSetupWizardTests capability tests."""

    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.secrets = MemorySecretStore()
        self.connection_calls: list[tuple[str, str, OdooConnectionMode | None]] = []
        self.schema_calls: list[tuple[str, str]] = []
        self.model_catalog_calls: list[tuple[str, str]] = []
        self.read_identity_calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.readiness_calls = []
        self.local_odoo_reader = MagicMock(spec=LocalOdooMetadataReader)

        def connection_tester(workspace_state, api_key):
            self.connection_calls.append(
                (workspace_state.workspace_id, api_key, workspace_state.odoo_connection_mode)
            )
            return _browser_schema(workspace_state).fingerprint

        def schema_reader(workspace_state, api_key):
            self.schema_calls.append((workspace_state.workspace_id, api_key))
            return _browser_schema(workspace_state)

        def model_catalog_reader(workspace_state, api_key):
            self.model_catalog_calls.append((workspace_state.workspace_id, api_key))
            return _browser_model_catalog(workspace_state)

        def read_identity_probe(workspace_state, api_key, models):
            normalized_models = tuple(sorted(models))
            self.read_identity_calls.append(
                (workspace_state.workspace_id, api_key, normalized_models)
            )
            return OdooReadIdentity(
                target_hash=target_identity_hash(
                    connection_mode=workspace_state.odoo_connection_mode.value,
                    base_url=workspace_state.odoo_base_url,
                    database=workspace_state.odoo_database,
                ),
                principal_hash="sha256:" + "1" * 64,
                permission_hash=(
                    "sha256:"
                    + ("2" if normalized_models == ("ir.model",) else "3") * 64
                ),
                context_hash="sha256:" + "4" * 64,
                readable_models=normalized_models,
                observed_at="2026-08-12T00:00:00Z",
            )

        def readiness_reader(workspace_state, metadata_requests, record_requests):
            self.readiness_calls.append(
                (workspace_state.workspace_id, metadata_requests, record_requests)
            )
            available_metadata = _browser_schema(workspace_state)
            metadata = replace(
                available_metadata,
                models={
                    request.model: replace(
                        available_metadata.models[request.model],
                        fields={
                            field: available_metadata.models[
                                request.model
                            ].fields[field]
                            for field in request.fields
                            if field
                            in available_metadata.models[request.model].fields
                        },
                    )
                    for request in metadata_requests
                    if request.model in available_metadata.models
                },
            )
            records = RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={item.model: () for item in record_requests},
                requested_fields={
                    item.model: item.fields for item in record_requests
                },
            )
            return metadata, records

        self.app = create_local_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=self.secrets,
            connection_tester=connection_tester,
            read_identity_probe=read_identity_probe,
            schema_reader=schema_reader,
            model_catalog_reader=model_catalog_reader,
            readiness_reader=readiness_reader,
            local_odoo_reader=self.local_odoo_reader,
        )
        self.workspaces = ProjectWorkspaceBuilder(self.app.state.context)
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

    def _complete_setup_before_target(self, workspace_id: str):
        """Reach the deferred Odoo destination step for a file project."""

        context = self.app.state.context
        workspace_state = context.queries.get(workspace_id)
        context.intake.accept(
            workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            display_name="target-test.csv",
            stream=BytesIO(b"code,name\nP001,Example\n"),
        )
        return context.workspace_states.register(
            workspace_id,
            actor=context.actor,
            expected_revision=context.queries.get(workspace_id).revision,
        )

    def _replace_connection_probes(
        self,
        *,
        fingerprint_probe=None,
        identity_probe=None,
    ) -> None:
        """Keep the shared connection service aligned with replaced test seams."""

        context = self.app.state.context
        if fingerprint_probe is not None:
            context.connection_tester = fingerprint_probe
        if identity_probe is not None:
            context.read_identity_probe = identity_probe
        context.odoo_connection_tests = OdooConnectionTestService(
            context.connection_tester,
            context.read_identity_probe,
        )

    def _registered_remote_schema_workspace(self):
        context = self.app.state.context
        created = self.workspaces.create(
            name="Remote summary recovery",
            source_system="Odoo",
            source_mode="ODOO",
        )
        now = datetime.now(timezone.utc)
        workspace_state = replace(
            created,
            source_mode=SourceMode.ODOO,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://remote.example.test",
            odoo_database="production",
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
            revision=created.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            workspace_state,
            expected_revision=created.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        _replace_run_target_setup(
            context,
            workspace_state.workspace_id,
            connection_mode=OdooConnectionMode.REMOTE,
            base_url="https://remote.example.test",
            database="production",
        )
        snapshot = _browser_schema(workspace_state)
        model = snapshot.models["res.partner"]
        schema = OdooSchemaCatalog(
            workspace_id=workspace_state.workspace_id,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=now,
            captured_by=context.actor.identity.display_name,
            connection_mode=workspace_state.odoo_connection_mode.value,
            database=workspace_state.odoo_database,
            odoo_version=snapshot.fingerprint.odoo_version,
            models=(
                SchemaModel(
                    model.model,
                    model.description or model.model,
                    tuple(
                        SchemaField(
                            name=field.name,
                            label=field.label or field.name,
                            type=field.type,
                            required=field.required,
                            readonly=field.readonly,
                            relation=field.relation,
                            relation_field=field.relation_field,
                            selection=field.selection,
                            stored=field.stored,
                            computed=field.computed,
                            has_inverse=field.has_inverse,
                            related=field.related,
                            translated=field.translated,
                            company_dependent=field.company_dependent,
                            searchable=field.searchable,
                            sortable=field.sortable,
                            exportable=field.exportable,
                            digits=field.digits,
                            currency_field=field.currency_field,
                        )
                        for _, field in sorted(model.fields.items())
                    ),
                    model.unique_constraints,
                ),
            ),
            content_hash="sha256:" + "5" * 64,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash="sha256:" + "6" * 64,
            read_principal_hash="sha256:" + "1" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            connection_target_hash=snapshot.fingerprint.target_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            schema,
            actor=context.actor,
        )
        return workspace_state, schema

    def _mapping_ready_workspace(
        self,
        *,
        scalar_field_count: int,
        relationship_field_count: int = 0,
        relationship_model: str = "res.partner",
        selection_field: bool = False,
        numeric_field: bool = False,
        readonly_scalar_indexes: tuple[int, ...] = (),
        readonly_relationship_indexes: tuple[int, ...] = (),
        required_scalar_indexes: tuple[int, ...] = (),
        verified_default_scalar_indexes: tuple[int, ...] = (),
        required_relationship_indexes: tuple[int, ...] = (),
        relationship_field_type: str = "many2one",
        business_key_description: str = "Unique reference",
        target_model: str = "res.partner",
        relationship_field_names: tuple[str, ...] | None = None,
        connection_mode: OdooConnectionMode = OdooConnectionMode.LOCAL,
    ):
        context = self.app.state.context
        created = self.workspaces.create(
            name="Large mapping",
            source_system="CSV",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            created,
            odoo_connection_mode=connection_mode,
            odoo_base_url=(
                "http://127.0.0.1:8069"
                if connection_mode is OdooConnectionMode.LOCAL
                else "https://remote.example.test"
            ),
            odoo_database="odoo19_local",
            intended_models=(target_model,),
            status=WorkspaceStatus.REGISTERED,
            revision=2,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            registered,
            expected_revision=created.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        _replace_run_target_setup(
            context,
            registered.workspace_id,
            connection_mode=connection_mode,
            base_url=registered.odoo_base_url,
            database="odoo19_local",
        )
        read_credential_binding_hash = "sha256:" + "1" * 64
        if connection_mode is OdooConnectionMode.REMOTE:
            read_credential_binding_hash = store_target_credential(
                self.secrets,
                registered,
                TargetCredentialRole.READ,
                "read-secret",
                persistent=False,
            ).binding_hash
        dataset = SourceDataset(
            dataset_id="dataset:" + "a" * 24,
            name="large_contacts",
            source=FileSourceBinding(
                file_id="source:large",
                table_key="contacts",
                source_sha256="sha256:" + "1" * 64,
                catalog_hash="sha256:" + "2" * 64,
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            ),
            row_count=1,
            columns=(
                SourceDatasetColumn(1, "Reference", "column:ref", "string"),
                SourceDatasetColumn(2, "Value", "column:value", "string"),
            ),
        )
        migration_workspace = context.migration_workspaces.get(
            registered.workspace_id,
            actor=context.actor,
        )
        data_version = context.data_versions.get(
            migration_workspace.data_version_id,
            actor=context.actor,
        )
        package_service = context.data_version_source_projection.packages
        current_package = package_service.repository.get_source_package(
            data_version.data_version_id
        )
        assert current_package is not None
        source_file_id = str(uuid4())
        source_hash = "sha256:" + "1" * 64
        source_catalog = SourceFileCatalog(
            contract_version=CATALOG_CONTRACT_VERSION,
            file_id=source_file_id,
            display_name="large-contacts.csv",
            source_sha256=source_hash,
            source_size_bytes=128,
            format="CSV",
            inspected_at=now,
            encoding="utf-8",
            delimiter=",",
            tables=(
                SourceTableCatalog(
                    table_key="contacts",
                    name="large_contacts",
                    kind="CSV",
                    hidden=False,
                    header_row=1,
                    row_count=1,
                    column_count=2,
                    columns=(
                        _source_column_profile(1, "Reference", "P001"),
                        _source_column_profile(2, "Value", "Example"),
                    ),
                    preview_rows=(("P001", "Example"),),
                ),
            ),
        )
        package_catalog = SourcePackageCatalog(
            file_id=source_file_id,
            source_sha256=source_hash,
            payload=json.loads(source_catalog.to_json()),
        )
        package_source = FileSourceBinding(
            file_id=source_file_id,
            table_key="contacts",
            source_sha256=source_hash,
            catalog_hash=package_catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        )
        source_snapshot = SourceSnapshot.create(
            data_version_id=data_version.data_version_id,
            dataset_id=dataset.dataset_id,
            dataset_name=dataset.name,
            source=package_source,
            physical_selection_hash="sha256:" + "3" * 64,
            schema=SourceSnapshotSchema.create(
                SourceSnapshotColumn.create(
                    ordinal=column.ordinal,
                    stable_key=column.stable_key,
                    source_name=column.source_name,
                    candidate_type=column.candidate_type,
                )
                for column in dataset.columns
            ),
            row_count=dataset.row_count,
            data_logical_hash="sha256:" + "5" * 64,
            parquet_sha256="sha256:" + "6" * 64,
            created_at=now,
        )
        package_dataset = SourcePackageDataset(
            dataset_id=dataset.dataset_id,
            display_name=dataset.name,
            source_file_ids=(source_file_id,),
            source=package_source,
            row_count=dataset.row_count,
            columns=dataset.columns,
            schema_hash=source_column_contract_hash(dataset.columns),
            snapshot_hash=source_snapshot.logical_hash,
            snapshot_storage_key=source_snapshot.parquet_storage_key,
            manifest={
                "logical_name": "large_contacts",
                "physical_selection_hash": "sha256:" + "3" * 64,
                "reader_contract_version": 2,
                "data_logical_hash": "sha256:" + "5" * 64,
                "parquet_sha256": "sha256:" + "6" * 64,
            },
        )
        dataset = package_dataset.to_mapping_dataset()
        draft_package = DataVersionSourcePackage(
            data_version_id=data_version.data_version_id,
            project_id=migration_workspace.project_id,
            revision=current_package.revision + 1,
            origin=SourcePackageOrigin.FILE,
            state=SourcePackageState.DRAFT,
            files=(
                SourcePackageFile(
                    file_id=source_file_id,
                    display_name="large-contacts.csv",
                    storage_key="source/large-contacts.csv",
                    size_bytes=128,
                    sha256=source_hash,
                    received_at=now,
                ),
            ),
            catalogs=(package_catalog,),
            configurations=(
                SourcePackageConfiguration(
                    file_id=source_file_id,
                    catalog_hash=package_catalog.content_hash,
                    payload={
                        "file_id": source_file_id,
                        "source_sha256": source_hash,
                        "catalog_hash": package_catalog.content_hash,
                        "encoding": "utf-8",
                        "delimiter": ",",
                        "selected_table_keys": ["contacts"],
                        "warnings_acknowledged": False,
                        "confirmed_at": now.isoformat(),
                        "confirmed_by": context.actor.identity.display_name,
                    },
                ),
            ),
            datasets=(package_dataset,),
            updated_at=now,
        )
        saved_package = package_service.replace_draft(
            draft_package,
            actor=context.actor,
            expected_package_revision=current_package.revision,
        )
        package_service.freeze(
            data_version.data_version_id,
            actor=context.actor,
            expected_data_version_revision=data_version.optimistic_revision,
            expected_package_revision=saved_package.revision,
            operation_id=str(uuid4()),
        )
        context.data_version_source_projection.projections.materialize(
            registered.workspace_id,
            actor=context.actor,
            dataset_ids=(dataset.dataset_id,),
            expected_workspace_revision=(
                migration_workspace.optimistic_revision
            ),
            operation_id=str(uuid4()),
        )
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=data_version.data_version_id,
            created_at=now,
            created_by=context.actor.identity.display_name,
            datasets=(dataset,),
            content_hash="sha256:" + "3" * 64,
        )
        context.sources.sources.save_source_selection(
            registered.workspace_id,
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
                        else (
                            "monetary"
                            if numeric_field and index == 0
                            else "char"
                        )
                    ),
                    required=index in required_scalar_indexes,
                    readonly=index in readonly_scalar_indexes,
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
                    create_default_present=(
                        index in verified_default_scalar_indexes
                    ),
                    create_default_value=(
                        "fr_FR"
                        if selection_field and index == 0
                        else f"Odoo default {index:04d}"
                    )
                    if index in verified_default_scalar_indexes
                    else None,
                )
                for index in range(scalar_field_count)
            ),
            *(
                SchemaField(
                    name=(
                        relationship_field_names[index]
                        if relationship_field_names is not None
                        else f"relation_{index:04d}"
                    ),
                    label=f"Linked Field {index:04d}",
                    type=relationship_field_type,
                    required=index in required_relationship_indexes,
                    readonly=index in readonly_relationship_indexes,
                    relation=relationship_model,
                    relation_field=None,
                    selection=(),
                )
                for index in range(relationship_field_count)
            ),
        )
        schema = OdooSchemaCatalog(
            workspace_id=registered.workspace_id,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=now,
            captured_by=context.actor.identity.display_name,
            connection_mode=registered.odoo_connection_mode.value,
            database=registered.odoo_database,
            odoo_version="19.0",
            models=(SchemaModel(target_model, target_model, fields),),
            content_hash="sha256:" + "4" * 64,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash="sha256:" + "1" * 64,
            read_permission_hash="sha256:" + "2" * 64,
            read_context_hash="sha256:" + "3" * 64,
            connection_target_hash=_browser_schema(registered).fingerprint.target_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            registered.workspace_id,
            schema,
            actor=context.actor,
        )
        business_key = BusinessKeyDefinition(
            key_id=f"{target_model}:ref",
            model=target_model,
            key_fields=("ref",),
            description=business_key_description,
            status=BusinessKeyStatus.CONFIRMED,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            workspace_id=registered.workspace_id,
            catalog_hash=schema.content_hash,
            permitted_models=(target_model,),
            business_keys=(business_key,),
            recorded_at=now,
            recorded_by=context.actor.identity.display_name,
        )
        context.schema_workspace.schemas.save_schema_governance(
            registered.workspace_id,
            governance,
            actor=context.actor,
        )
        return registered.workspace_id, dataset, business_key

    def _post(self, path: str, data: dict[str, str]):
        submitted = dict(data)
        if path == "/projects/new":
            submitted.setdefault("creation_request_id", str(uuid4()))
        return self.client.post(
            path,
            data=submitted,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
