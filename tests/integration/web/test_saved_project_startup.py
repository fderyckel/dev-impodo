"""Keep new local Projects usable across application restarts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import duckdb
from fastapi.testclient import TestClient

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.schema.data_version_store import (
    DATA_VERSION_STORE_GENERATION,
    DATA_VERSION_STORE_VERSION,
)
from impodo.adapters.duckdb.schema.migration_registry import (
    MIGRATION_REGISTRY_GENERATION,
    MIGRATION_REGISTRY_VERSION,
)
from impodo.adapters.duckdb.schema.migration_workspace_store import (
    MIGRATION_WORKSPACE_GENERATION,
    MIGRATION_WORKSPACE_VERSION,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.domain.shared.schema import WORKSPACE_SCHEMA_GENERATION, WORKSPACE_SCHEMA_VERSION
from impodo.web.app import create_local_app
from tests.support.browser_scenarios import POST_HEADERS, _csrf
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


def _saved_rows(root: Path) -> dict:
    """Retain all existing business rows while ignoring compatibility metadata."""

    rows = {}
    for path in root.rglob("*.duckdb"):
        with duckdb.connect(str(path), read_only=True) as connection:
            for (table,) in connection.execute("SHOW TABLES").fetchall():
                if table in {"schema_version", "schema_migration"}:
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                rows[(path.relative_to(root), table)] = sorted(
                    connection.execute(f"SELECT * FROM {quoted}").fetchall(),
                    key=repr,
                )
    return rows


class SavedProjectStartupTests(unittest.TestCase):
    def test_restart_retains_projects_created_from_the_first_project_baseline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            secrets = MemorySecretStore()
            app = create_local_app(
                root, secret_store=secrets, launch_token="saved-project-launch"
            )
            context = app.state.context
            created = context.project_authoring.create(
                actor=resolve_local_actor(context),
                display_name="Saved fictional customer project",
                source_mode="FILE",
                creation_request_id=str(uuid4()),
                source_system_identity="Fictional ERP",
            )
            workspace_id = created.workspace_state.workspace_id
            with TestClient(app) as client:
                page = client.get("/launch?token=saved-project-launch")
                self.assertEqual(page.status_code, 200)
                uploaded = client.post(
                    f"/workspaces/{workspace_id}/files",
                    data={"csrf_token": _csrf(page.text), "revision": "1"},
                    files={
                        "source_file": (
                            "customers.csv",
                            b"code,name\nC001,Fictional customer\n",
                            "text/csv",
                        )
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(uploaded.status_code, 303)
            before = _saved_rows(root)
            schemas = {
                "registry.duckdb": (MIGRATION_REGISTRY_GENERATION, MIGRATION_REGISTRY_VERSION),
                "data-version.duckdb": (DATA_VERSION_STORE_GENERATION, DATA_VERSION_STORE_VERSION),
                "workspace.duckdb": (MIGRATION_WORKSPACE_GENERATION, MIGRATION_WORKSPACE_VERSION),
                "workspace-engine.duckdb": (WORKSPACE_SCHEMA_GENERATION, WORKSPACE_SCHEMA_VERSION),
            }
            self.assertEqual(
                {path.name for path in root.rglob("*.duckdb")}, set(schemas)
            )
            for restart in range(2):
                with self.subTest(restart=restart):
                    app = create_local_app(
                        root, secret_store=secrets, launch_token="saved-project-launch"
                    )
                    with TestClient(app) as client:
                        projects = client.get("/launch?token=saved-project-launch")
                        self.assertEqual(projects.status_code, 200)
                        self.assertIn("Saved fictional customer project", projects.text)
                        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
                            files = client.get(f"/workspaces/{workspace_id}/files")
                        self.assertEqual(files.status_code, 200)
                        self.assertIn("customers.csv", files.text)
                        # Admission and rendering share registry, reference,
                        # engine, and Data version owners in one operation.
                        self.assertLessEqual(timings.connection_count, 4)
                        self.assertEqual(timings.schema_check_count, 3)
                        self.assertEqual(
                            sum("duckdb_columns()" in sql for sql in statements),
                            timings.schema_check_count,
                        )
                        self.assertEqual(timings.lock_retry_count, 0)
                    self.assertEqual(_saved_rows(root), before)
                    for path in root.rglob("*.duckdb"):
                        with duckdb.connect(str(path), read_only=True) as connection:
                            self.assertEqual(
                                connection.execute(
                                    "SELECT generation, version FROM schema_version"
                                ).fetchone(),
                                schemas[path.name],
                            )
                            self.assertEqual(
                                connection.execute(
                                    "SELECT * FROM schema_migration"
                                ).fetchall(),
                                [],
                            )
