"""Verify workspace-evidence and artifact ownership."""

from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from io import BytesIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.domain.project.foundation import MigrationStorageCompatibilityError
from impodo.web.app import create_local_app
from impodo.domain.workspace.contracts import SourceSelection


ROOT = REPOSITORY_ROOT


class WorkspaceEvidenceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"workspace-evidence-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_artifact_roots_separate_data_versions_from_workspaces(self) -> None:
        store = LocalArtifactStore(self.root / "artifacts")
        data_version_id = str(uuid4())
        workspace_id = str(uuid4())
        source = store.store_source(
            data_version_id,
            artifact_id=str(uuid4()),
            suffix=".csv",
            stream=BytesIO(b"name\nAda\n"),
            maximum_bytes=1_000,
            chunk_bytes=16,
            validator=lambda _path: None,
        )
        with store.prepare_prepared_snapshot(workspace_id) as work:
            self.assertIn(workspace_id, work.parts)
            self.assertIn("ws", work.parts)

        self.assertTrue(
            (
                self.root
                / "artifacts"
                / "dv"
                / data_version_id
                / "inbox"
                / source.storage_key
            ).is_file()
        )
        self.assertFalse((self.root / "artifacts" / data_version_id).exists())
        self.assertFalse((self.root / "artifacts" / workspace_id).exists())

    def test_retired_source_selection_identity_is_rejected(self) -> None:
        retired_payload = {
            "contract_version": 1,
            "selection_id": str(uuid4()),
            "version": 1,
            "project_id": str(uuid4()),
            "created_at": "2026-08-24T00:00:00+00:00",
            "created_by": "Data manager",
            "datasets": [],
            "content_hash": "sha256:" + "0" * 64,
        }
        with self.assertRaises(ValueError):
            SourceSelection.from_json(json.dumps(retired_payload))

    def test_linkage_mismatch_fails_before_workspace_engine_read(self) -> None:
        app = create_local_app(
            self.root,
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
            load_jobs_enabled=False,
        )
        context = app.state.context
        bundle = context.project_authoring.create(
            actor=context.actor,
            display_name="Exact lineage",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )
        workspace_store = (
            self.root
            / "projects"
            / bundle.project.project_id
            / "workspaces"
            / bundle.workspace.workspace_id
            / "workspace.duckdb"
        )
        with duckdb.connect(str(workspace_store)) as connection:
            connection.execute(
                "UPDATE workspace_linkage SET project_id = ?",
                [str(uuid4())],
            )

        repository = context.workspace_states.repository
        database = repository._database
        with patch.object(
            database,
            "_connect",
            side_effect=AssertionError("workspace engine evidence was opened"),
        ) as engine_connect:
            with self.assertRaises(MigrationStorageCompatibilityError):
                repository.get(bundle.workspace.workspace_id)
        engine_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
