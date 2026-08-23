"""Verify automatic preservation and UI treatment of older Projects."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import duckdb
from fastapi.testclient import TestClient

from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.incompatible_project_storage import (
    LEGACY_RECIPE_ROOT_MIGRATION_CHECKSUM,
    LEGACY_RECIPE_ROOT_MIGRATION_ID,
    UNAVAILABLE_PROJECT_MESSAGE,
    prepare_incompatible_project_storage,
)
from impodo.migration_foundation import MigrationStorageCompatibilityError
from impodo.secrets import MemorySecretStore
from impodo.web.app import create_local_app


ROOT = Path(__file__).resolve().parents[1]


class IncompatibleProjectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"incompatible-project-storage-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_known_older_projects_are_preserved_once_and_listed_without_db_reads(
        self,
    ) -> None:
        first_id = str(uuid4())
        second_id = str(uuid4())
        _write_known_legacy_root(
            self.root,
            (
                (
                    first_id,
                    "Customer rehearsal",
                    "REGISTERED",
                    8,
                    "2026-08-21T10:48:26+00:00",
                ),
                (
                    second_id,
                    "Product draft",
                    "DRAFT",
                    1,
                    "2026-08-19T13:28:24+00:00",
                ),
            ),
        )

        summaries = prepare_incompatible_project_storage(self.root)

        self.assertEqual(
            [item.display_name for item in summaries],
            ["Customer rehearsal", "Product draft"],
        )
        self.assertTrue(
            all(item.message == UNAVAILABLE_PROJECT_MESSAGE for item in summaries)
        )
        self.assertFalse((self.root / "registry.duckdb").exists())
        self.assertFalse((self.root / first_id).exists())
        archives = tuple((self.root / ".impodo-development-reset").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "registry.duckdb").is_file())
        self.assertTrue((archives[0] / first_id / "project.duckdb").is_file())
        (archives[0] / first_id / "project.duckdb").unlink()

        restarted = prepare_incompatible_project_storage(self.root)

        self.assertEqual(restarted, summaries)
        self.assertEqual(
            len(tuple((self.root / ".impodo-development-reset").iterdir())),
            1,
        )

    def test_unknown_entry_keeps_the_entire_older_root_unchanged(self) -> None:
        project_id = str(uuid4())
        _write_known_legacy_root(
            self.root,
            (
                (
                    project_id,
                    "Project with notes",
                    "DRAFT",
                    1,
                    "2026-08-19T13:28:24+00:00",
                ),
            ),
        )
        notes = self.root / "operator-notes.txt"
        notes.write_text("Keep me", encoding="utf-8")

        self.assertEqual(prepare_incompatible_project_storage(self.root), ())

        self.assertTrue((self.root / "registry.duckdb").is_file())
        self.assertTrue((self.root / project_id / "project.duckdb").is_file())
        self.assertEqual(notes.read_text(encoding="utf-8"), "Keep me")
        with self.assertRaises(MigrationStorageCompatibilityError):
            MigrationFoundationDatabase(self.root)

    def test_successive_foundation_generation_is_preserved_with_older_cards(
        self,
    ) -> None:
        legacy_project_id = str(uuid4())
        foundation_project_id = str(uuid4())
        _write_known_legacy_root(
            self.root,
            (
                (
                    legacy_project_id,
                    "Original rehearsal",
                    "REGISTERED",
                    8,
                    "2026-08-21T10:48:26+00:00",
                ),
            ),
        )
        self.assertEqual(
            len(prepare_incompatible_project_storage(self.root)),
            1,
        )
        _write_prior_foundation_root(
            self.root,
            foundation_project_id,
            "Newer customer draft",
        )

        summaries = prepare_incompatible_project_storage(self.root)

        self.assertEqual(
            {item.display_name for item in summaries},
            {"Original rehearsal", "Newer customer draft"},
        )
        archives = tuple((self.root / ".impodo-development-reset").iterdir())
        self.assertEqual(len(archives), 2)
        foundation_archive = next(
            archive for archive in archives if (archive / "projects").is_dir()
        )
        self.assertTrue(
            (
                foundation_archive
                / "projects"
                / foundation_project_id
                / "workspace-evidence.txt"
            ).is_file()
        )
        self.assertTrue(
            (
                foundation_archive
                / ".project-evidence-protected"
                / "protected.txt"
            ).is_file()
        )
        self.assertTrue(
            (foundation_archive / "artifacts" / "artifact.txt").is_file()
        )
        self.assertFalse((self.root / "registry.duckdb").exists())
        self.assertFalse((self.root / "projects").exists())

    def test_projects_page_explains_why_an_older_project_is_unavailable(self) -> None:
        project_id = str(uuid4())
        _write_known_legacy_root(
            self.root,
            (
                (
                    project_id,
                    "Historical customer rehearsal",
                    "REGISTERED",
                    8,
                    datetime(2026, 8, 21, 10, 48, tzinfo=timezone.utc).isoformat(),
                ),
            ),
        )
        app = create_local_app(
            self.root,
            launch_token="legacy-launch",
            session_secret="legacy-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        with TestClient(app) as client:
            launched = client.get(
                "/launch?token=legacy-launch",
                follow_redirects=False,
            )
            self.assertEqual(launched.status_code, 303)

            listing = client.get("/projects")

        self.assertEqual(listing.status_code, 200)
        self.assertIn("Historical customer rehearsal", listing.text)
        self.assertIn("Unavailable", listing.text)
        self.assertIn(UNAVAILABLE_PROJECT_MESSAGE, listing.text)
        self.assertIn("data-unavailable-project", listing.text)
        self.assertNotIn(f'href="/projects/{project_id}"', listing.text)
        self.assertIn("New project", listing.text)
        self.assertTrue((self.root / "registry.duckdb").is_file())
        self.assertTrue((self.root / "projects").is_dir())


def _write_known_legacy_root(
    root: Path,
    projects: tuple[tuple[str, str, str, int, str], ...],
) -> None:
    with duckdb.connect(str(root / "registry.duckdb")) as connection:
        connection.execute(
            """
            CREATE TABLE registry_schema_migration (
                migration_id VARCHAR PRIMARY KEY,
                checksum VARCHAR NOT NULL,
                applied_at VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO registry_schema_migration VALUES (?, ?, ?)",
            [
                LEGACY_RECIPE_ROOT_MIGRATION_ID,
                LEGACY_RECIPE_ROOT_MIGRATION_CHECKSUM,
                "2026-08-19T00:00:00+00:00",
            ],
        )
        connection.execute(
            """
            CREATE TABLE project_registry (
                project_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                updated_at VARCHAR NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO project_registry VALUES (?, ?, ?, ?, ?)",
            projects,
        )
    for project_id, name, status, revision, updated_at in projects:
        project_root = root / project_id
        project_root.mkdir()
        with duckdb.connect(str(project_root / "project.duckdb")) as connection:
            connection.execute(
                """
                CREATE TABLE project (
                    project_id VARCHAR,
                    name VARCHAR,
                    status VARCHAR,
                    revision INTEGER,
                    updated_at VARCHAR
                )
                """
            )
            connection.execute(
                "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
                [project_id, name, status, revision, updated_at],
            )


def _write_prior_foundation_root(
    root: Path,
    project_id: str,
    display_name: str,
) -> None:
    with duckdb.connect(str(root / "registry.duckdb")) as connection:
        connection.execute(
            """
            CREATE TABLE schema_version (
                singleton_id INTEGER PRIMARY KEY,
                generation VARCHAR NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_version VALUES (1, ?, 1)",
            ["impodo-migration-registry-2026-08-m5"],
        )
        connection.execute(
            """
            CREATE TABLE migration_project (
                project_id VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                optimistic_revision INTEGER NOT NULL,
                updated_at VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO migration_project VALUES (?, ?, 'DRAFT', 4, ?)",
            [
                project_id,
                display_name,
                "2026-08-22T23:54:49+00:00",
            ],
        )
    project_root = root / "projects" / project_id
    project_root.mkdir(parents=True)
    (project_root / "workspace-evidence.txt").write_text(
        "preserved",
        encoding="utf-8",
    )
    protected = root / ".project-evidence-protected"
    protected.mkdir()
    (protected / "protected.txt").write_text("preserved", encoding="utf-8")
    artifacts = root / "artifacts"
    artifacts.mkdir()
    (artifacts / "artifact.txt").write_text("preserved", encoding="utf-8")
    (root / ".recipes-protected").mkdir()
