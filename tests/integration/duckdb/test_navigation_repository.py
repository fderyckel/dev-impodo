"""Bounded DuckDB evidence used by shared workflow navigation."""

from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.navigation_repository import (
    WorkspaceNavigationRepository,
)
from impodo.adapters.duckdb.schema.workspace_engine import (
    WorkspaceEngineSchemaMixin,
)
from impodo.adapters.duckdb.serialization import _workspace_values
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState


HASH = "sha256:" + "2" * 64


class _WorkspaceSchema(WorkspaceEngineSchemaMixin):
    pass


@contextmanager
def _keep_open(connection):
    yield connection


class WorkspaceNavigationRepositoryTests(unittest.TestCase):
    def test_overview_snapshot_uses_one_checked_workspace_connection(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            _WorkspaceSchema()._initialize_workspace_database(connection)
            workspace = WorkspaceState(
                workspace_id=str(uuid4()),
                name="Products",
                source_system="CSV",
                source_mode=SourceMode.FILE,
            )
            connection.execute(
                f"INSERT INTO workspace_projection_cache VALUES "
                f"({', '.join('?' for _ in range(32))})",
                _workspace_values(workspace),
            )
            directory = MagicMock()
            database_path = MagicMock()
            directory.__truediv__.return_value = database_path
            database_path.is_file.return_value = True
            repository = object.__new__(WorkspaceNavigationRepository)
            repository.workspace_directory = Mock(return_value=directory)
            repository._connect = Mock(
                side_effect=lambda _path: _keep_open(connection)
            )
            repository._ensure_workspace_database_schema = Mock()

            snapshot = repository.get_snapshot(workspace.workspace_id)

            self.assertEqual(snapshot.workspace_state, workspace)
            self.assertEqual(snapshot.facts.workspace_id, workspace.workspace_id)
            repository._connect.assert_called_once_with(database_path)
            repository._ensure_workspace_database_schema.assert_called_once_with(
                connection
            )
        finally:
            connection.close()

    def test_large_invalid_owned_artifacts_are_not_decoded_for_navigation(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            _WorkspaceSchema()._initialize_workspace_database(connection)
            run_id = str(uuid4())
            connection.execute(
                "INSERT INTO preflight_current VALUES (1, ?)",
                [run_id],
            )
            connection.execute(
                """
                INSERT INTO preflight_execution_projection VALUES (
                    ?, ?, ?, 'READY', 3, 2, 1, 0, 0, 0,
                    ?, '19.0', '', '', '', '', TRUE, 1
                )
                """,
                [run_id, HASH, HASH, HASH],
            )
            connection.execute(
                """
                INSERT INTO readiness_run (
                    run_id, mapping_id, mapping_version, mapping_content_hash,
                    target_hash, staging_run_id, staging_content_hash,
                    quality_run_id, quality_content_hash, checked_at,
                    checked_by, report_json
                ) VALUES (?, 'mapping', 1, ?, ?, 'staging', ?, 'quality', ?,
                          '2026-09-14T12:00:00+00:00', 'tester', ?)
                """,
                [run_id, HASH, HASH, HASH, HASH, "invalid:" + "x" * 1_000_000],
            )
            connection.execute(
                """
                INSERT INTO preflight_target_snapshot
                VALUES (?, 'records', ?, ?)
                """,
                [run_id, HASH, "invalid:" + "y" * 1_000_000],
            )

            facts = WorkspaceNavigationRepository._read_facts(
                connection,
                "workspace-1",
            )
        finally:
            connection.close()

        self.assertIsNotNone(facts.execution_state)
        assert facts.execution_state is not None
        self.assertEqual(facts.execution_state.summary.write_count, 5)
        self.assertEqual(facts.execution_state.summary.snapshot_hash, HASH)

    def test_malformed_small_schema_evidence_fails_closed(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            _WorkspaceSchema()._initialize_workspace_database(connection)
            connection.execute(
                "INSERT INTO odoo_schema_catalog VALUES (1, 'not-json')"
            )

            facts = WorkspaceNavigationRepository._read_facts(
                connection,
                "workspace-1",
            )
        finally:
            connection.close()

        self.assertFalse(facts.schema_present)
        self.assertFalse(facts.schema_complete)
        self.assertTrue(facts.schema_attention)

    def test_invalid_execution_projection_is_not_treated_as_loadable(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            _WorkspaceSchema()._initialize_workspace_database(connection)
            run_id = str(uuid4())
            connection.execute(
                "INSERT INTO preflight_current VALUES (1, ?)",
                [run_id],
            )
            connection.execute(
                """
                INSERT INTO preflight_execution_projection VALUES (
                    ?, ?, ?, 'INVALID', 1, 0, 0, 0, 0, 0,
                    ?, '19.0', '', '', '', '', TRUE, 1
                )
                """,
                [run_id, HASH, HASH, HASH],
            )

            facts = WorkspaceNavigationRepository._read_facts(
                connection,
                "workspace-1",
            )
        finally:
            connection.close()

        self.assertIsNone(facts.execution_state)
        self.assertEqual(facts.preflight_status, "")


if __name__ == "__main__":
    unittest.main()
