"""Bounded DuckDB evidence used by shared workflow navigation."""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.navigation_repository import (
    WorkspaceNavigationRepository,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.schema.workspace_engine import (
    WorkspaceEngineSchemaMixin,
)
from impodo.adapters.duckdb.serialization import _workspace_values
from impodo.application.workspace.source_readiness import (
    SourceStageIssue,
    SourceStageReadiness,
    assess_source_stage_readiness,
)
from impodo.domain.matching_order import (
    MatchingIdentityCheckStatus,
    MatchingIdentityResult,
    MatchingOrderCheck,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateNotFoundError,
)
from tests.support.database_probe import count_duckdb_statements

HASH = "sha256:" + "2" * 64


class _WorkspaceSchema(WorkspaceEngineSchemaMixin):
    pass


@contextmanager
def _keep_open(connection):
    yield connection


def _matching_health_check() -> MatchingOrderCheck:
    return MatchingOrderCheck(
        check_id=str(uuid4()),
        workspace_id="workspace-1",
        source_selection_hash=HASH,
        schema_hash=HASH,
        governance_hash=HASH,
        working_draft_version=1,
        working_draft_hash=HASH,
        target_hash=HASH,
        read_credential_binding_hash=HASH,
        read_principal_hash=HASH,
        read_permission_hash=HASH,
        read_context_hash=HASH,
        relationship_results=(),
        unchecked_relationship_count=0,
        ordered_dataset_ids=(),
        recommendation_hash=HASH,
        schema_changed=False,
        captured_at=datetime.now(UTC),
        actor_issuer="local",
        actor_subject="operator",
        actor_display_name="Operator",
        identity_results=(
            MatchingIdentityResult(
                dataset_id="products",
                target_model="product.template",
                target_fields=("default_code",),
                status=MatchingIdentityCheckStatus.CHECKED,
                source_row_count=1,
                source_blank_row_count=1,
                blocked_count=1,
            ),
        ),
    )


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
            repository._source_projections = None
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
                    ?, '19.0', '', '', '', '', TRUE, 1, ''
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

    def test_current_matching_health_uses_bounded_counts_and_stale_bindings_hide_it(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        try:
            _WorkspaceSchema()._initialize_workspace_database(connection)
            check = _matching_health_check()
            connection.execute(
                "INSERT INTO source_selection VALUES (1, ?)",
                [json.dumps({"content_hash": HASH})],
            )
            connection.execute(
                "INSERT INTO odoo_schema_catalog VALUES (1, ?)",
                [
                    json.dumps(
                        {
                            "contract_version": "2",
                            "models": [],
                            "content_hash": HASH,
                            "connection_target_hash": HASH,
                            "read_credential_binding_hash": HASH,
                            "read_principal_hash": HASH,
                            "read_permission_hash": HASH,
                            "read_context_hash": HASH,
                            "pending_refresh": None,
                        }
                    )
                ],
            )
            connection.execute(
                """
                INSERT INTO mapping_working_draft
                VALUES (1, 'mapping', 1, ?, ?, ?, '', '{}')
                """,
                [HASH, HASH, HASH],
            )
            connection.execute(
                """
                INSERT INTO matching_order_check
                VALUES (?, '', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    check.check_id,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    HASH,
                    check.to_json(),
                ],
            )
            connection.execute(
                "INSERT INTO matching_order_check_current VALUES (1, ?)",
                [check.check_id],
            )

            current = WorkspaceNavigationRepository._read_facts(
                connection,
                "workspace-1",
            )
            connection.execute(
                "UPDATE mapping_working_draft SET content_hash = ?",
                ["sha256:" + "3" * 64],
            )
            stale = WorkspaceNavigationRepository._read_facts(
                connection,
                "workspace-1",
            )
        finally:
            connection.close()

        self.assertEqual(current.workflow_issue_must_fix_count, 1)
        self.assertEqual(current.workflow_issue_review_count, 0)
        self.assertEqual(stale.workflow_issue_must_fix_count, 0)
        self.assertEqual(stale.workflow_issue_review_count, 0)

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
                    ?, '19.0', '', '', '', '', TRUE, 1, ''
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


class SourceReadinessRepositoryTests(unittest.TestCase):
    def setUp(self):
        database = DuckDbWorkspaceDatabase(self.enterContext(TemporaryDirectory()))
        self.workspace_id = str(uuid4())
        directory = database.workspace_directory(self.workspace_id)
        directory.mkdir()
        self.db = self.enterContext(database._connect(directory / "workspace-engine.duckdb"))
        database._initialize_workspace_database(self.db)
        state = WorkspaceState(
            workspace_id=self.workspace_id, name="Contacts", source_system="CSV",
        )
        values = _workspace_values(state)
        parameters = ", ".join("?" for _ in values)
        self.db.execute(f"INSERT INTO workspace_projection_cache VALUES ({parameters})", values)

        self.projections = Mock()
        self.projections.get_workspace_source_projection.return_value = None
        self.destination = Mock()
        self.repository = WorkspaceNavigationRepository(database, self.projections, self.destination)

    def assert_gate(self, mode, issue=None, missing=()):
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            source_mode, facts = self.repository.get_source_readiness_inputs(self.workspace_id)
        self.assertEqual(source_mode, mode)
        readiness = assess_source_stage_readiness(source_mode, facts)
        self.assertEqual(readiness, SourceStageReadiness(issue is None, issue, missing))
        self.assertEqual((timings.connection_count, timings.schema_check_count), (1, 1))
        self.destination.assert_not_called()
        sql = " ".join(" ".join(statement.lower().split()) for statement in statements)
        self.assertNotRegex(
            sql,
            r"\b(?:from|join) (?:source_file|source_configuration|mapping_\w+|"
            r"normalization_\w+|preflight_\w+|transfer_navigation)\b",
        )
        if mode is SourceMode.FILE:
            self.assertNotRegex(sql, r"\b(?:from|join) odoo_(?:schema_catalog|capture_selection_current|model_catalog)\b")
        else:
            full = WorkspaceNavigationRepository._read_facts(self.db, self.workspace_id)
            self.assertEqual(readiness, assess_source_stage_readiness(mode, full))

    def test_missing_engine_and_missing_singleton_reject_the_source_gate(self):
        with self.assertRaises(WorkspaceStateNotFoundError):
            self.repository.get_source_readiness_inputs(str(uuid4()))
        self.db.execute("DELETE FROM workspace_projection_cache")
        with self.assertRaises(WorkspaceError):
            self.repository.get_source_readiness_inputs(self.workspace_id)

    def test_file_gate_uses_local_selection_or_verified_recipe_projection(self):
        self.assert_gate(SourceMode.FILE, SourceStageIssue.FILE_SOURCE_NOT_FROZEN)
        projection = self.projections.get_workspace_source_projection
        projection.assert_called_once_with(self.workspace_id)

        self.db.execute("INSERT INTO source_selection VALUES (1, 'not-json')")
        self.assert_gate(SourceMode.FILE, SourceStageIssue.FILE_SOURCE_NOT_FROZEN)
        selected = SimpleNamespace(manifest={"physical_selection_hash": HASH})
        projection.return_value = SimpleNamespace(datasets=(selected, selected))
        self.assert_gate(SourceMode.FILE)

        self.db.execute("UPDATE source_selection SET selection_json = ?", [json.dumps({"content_hash": HASH})])
        projection.reset_mock()
        self.assert_gate(SourceMode.FILE)
        projection.assert_not_called()

    def test_recipe_rejects_missing_or_conflicting_source_identities(self):
        missing = SimpleNamespace(manifest={})
        selected = SimpleNamespace(manifest={"physical_selection_hash": HASH})
        other = SimpleNamespace(manifest={"physical_selection_hash": "sha256:" + "3" * 64})
        for datasets in ((), (missing,), (selected, other)):
            with self.subTest(datasets=datasets), self.assertRaises(WorkspaceError):
                self.projections.get_workspace_source_projection.return_value = SimpleNamespace(datasets=datasets)
                self.repository.get_source_readiness_inputs(self.workspace_id)

    def configure_odoo_source(self):
        self.db.execute("""
            UPDATE workspace_projection_cache
               SET source_mode = 'ODOO', destination_odoo_connection_mode = 'REMOTE',
                   destination_odoo_base_url = 'https://odoo.example.test',
                   destination_odoo_database = 'target'
        """)
        catalog = {
            "contract_version": "2",
            "content_hash": HASH,
            "models": [{"name": "res.partner"}, {"name": "uom.uom"}],
        }
        self.db.execute("INSERT INTO odoo_model_catalog VALUES (1, ?)", [json.dumps(catalog)])
        return catalog

    def test_odoo_needs_current_schema_even_with_model_discovery(self):
        valid = self.configure_odoo_source()
        self.assert_gate(SourceMode.ODOO, SourceStageIssue.SCHEMA_MISSING)
        for catalog in (
            "not-json",
            json.dumps({**valid, "contract_version": 3}),
            json.dumps({**valid, "models": None}),
            json.dumps({**valid, "content_hash": "invalid"}),
        ):
            with self.subTest(catalog=catalog):
                self.db.execute("INSERT OR REPLACE INTO odoo_schema_catalog VALUES (1, ?)", [catalog])
                self.assert_gate(SourceMode.ODOO, SourceStageIssue.SCHEMA_MISSING)
        pending = {**valid, "pending_refresh": {"fields": []}}
        self.db.execute("UPDATE odoo_schema_catalog SET catalog_json = ?", [json.dumps(pending)])
        self.assert_gate(SourceMode.ODOO, SourceStageIssue.SCHEMA_ATTENTION)

    def test_odoo_plans_must_match_selected_models_before_freeze(self):
        catalog = self.configure_odoo_source()
        self.db.execute("INSERT INTO odoo_schema_catalog VALUES (1, ?)", [json.dumps(catalog)])
        selected = ("res.partner", "uom.uom")
        incomplete = SourceStageIssue.CAPTURE_PLANS_INCOMPLETE
        self.assert_gate(SourceMode.ODOO, incomplete, selected)
        self.db.execute("INSERT INTO odoo_capture_selection_current VALUES ('uom.uom', 'a', 1)")
        self.assert_gate(SourceMode.ODOO, incomplete, ("res.partner",))
        self.db.execute("""
            INSERT INTO odoo_capture_selection_current VALUES
              ('res.partner', 'b', 1), ('old.model', 'c', 1)
        """)
        self.assert_gate(SourceMode.ODOO, incomplete)
        self.db.execute("DELETE FROM odoo_capture_selection_current WHERE model = 'old.model'")
        self.assert_gate(SourceMode.ODOO, SourceStageIssue.SOURCE_NOT_FROZEN)
        self.db.execute("INSERT INTO source_selection VALUES (1, ?)", [json.dumps({"content_hash": HASH})])
        self.assert_gate(SourceMode.ODOO)


if __name__ == "__main__":
    unittest.main()
