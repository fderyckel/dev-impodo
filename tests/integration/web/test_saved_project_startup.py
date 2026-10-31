"""Keep new local Projects usable across application restarts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import duckdb
from fastapi.testclient import TestClient

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.app import create_local_app
from tests.support.browser_scenarios import POST_HEADERS, _csrf
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
            generations = {
                "registry.duckdb": "impodo-migration-registry-2026-10-31-project-root",
                "data-version.duckdb": "impodo-data-version-store-2026-10-31-project-owned",
                "workspace.duckdb": "impodo-migration-workspace-2026-10-31-reference-only",
                "workspace-engine.duckdb": "impodo-workspace-engine-2026-10-31-workspace-owned",
            }
            self.assertEqual(
                {path.name for path in root.rglob("*.duckdb")}, set(generations)
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
                        with collect_duckdb_request_timings() as timings:
                            files = client.get(f"/workspaces/{workspace_id}/files")
                        self.assertEqual(files.status_code, 200)
                        self.assertIn("customers.csv", files.text)
                        # Route policy opens the registry and reference store.
                        # The retained page scope opens those two stores again,
                        # plus the engine and DataVersion store: two plus four.
                        self.assertLessEqual(timings.connection_count, 6)
                        self.assertEqual(timings.schema_check_count, 1)
                        self.assertEqual(timings.lock_retry_count, 0)
                    self.assertEqual(_saved_rows(root), before)
                    for path in root.rglob("*.duckdb"):
                        with duckdb.connect(str(path), read_only=True) as connection:
                            self.assertEqual(
                                connection.execute(
                                    "SELECT generation, version FROM schema_version"
                                ).fetchone(),
                                (generations[path.name], 1),
                            )
                            self.assertEqual(
                                connection.execute(
                                    "SELECT * FROM schema_migration"
                                ).fetchall(),
                                [],
                            )
