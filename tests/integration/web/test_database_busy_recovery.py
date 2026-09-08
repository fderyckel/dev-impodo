"""Browser recovery for transient local DuckDB handle contention."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb
from fastapi.testclient import TestClient

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.domain.workspace.errors import WorkspaceDatabaseBusyError
from impodo.web.app import (
    DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS,
    create_local_app,
)
from tests.support.paths import REPOSITORY_ROOT


LOCK_ERROR = (
    'IO Error: Cannot open file "registry.duckdb": The process cannot access '
    "the file because it is being used by another process."
)


@contextmanager
def _test_app():
    root = REPOSITORY_ROOT / ".tmp" / f"database-busy-{uuid4()}"
    root.mkdir(parents=True)
    app = create_local_app(
        root / "projects",
        launch_token="database-busy-launch-token",
        session_secret="database-busy-session-secret",
        secret_store=MemorySecretStore(),
        preparation_jobs_enabled=False,
        odoo_capture_jobs_enabled=False,
        load_jobs_enabled=False,
    )
    try:
        with TestClient(app) as client:
            client.get(
                "/launch?token=database-busy-launch-token",
                follow_redirects=False,
            )
            yield app, client
    finally:
        shutil.rmtree(root)


class BrowserDatabaseBusyRecoveryTests(unittest.TestCase):
    def test_browser_retries_one_transient_database_handle_failure(self) -> None:
        with _test_app() as (_app, client):
            real_connect = duckdb.connect
            calls = 0

            def transient_connect(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise duckdb.IOException(LOCK_ERROR)
                return real_connect(*args, **kwargs)

            with patch(
                "impodo.adapters.duckdb.unit_of_work.duckdb.connect",
                side_effect=transient_connect,
            ):
                response = client.get("/projects")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS, 2.0)

    def test_longer_database_contention_returns_a_retry_page(self) -> None:
        with _test_app() as (app, client):
            with patch.object(
                type(app.state.context.migration_projects),
                "list",
                side_effect=WorkspaceDatabaseBusyError("busy"),
            ):
                response = client.get("/projects")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertIn("Wait a moment, then reload this page", response.text)
        self.assertIn("No Odoo records were changed", response.text)


if __name__ == "__main__":
    unittest.main()
