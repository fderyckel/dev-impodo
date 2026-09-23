from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID, uuid4

import duckdb

from impodo.adapters.duckdb.preflight_repository import PreflightRepository
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.workspace.execution.navigation import ExecutionPreviewSummary
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import WorkspaceState
from tests.support.paths import REPOSITORY_ROOT


RUN_ID = "00000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


class _ExistingFileDirectory:
    def __truediv__(self, _name: str) -> Path:
        return Path(__file__)


class _InMemoryDatabase:
    """Provide the repository boundary needed by the bounded row reader."""

    root = Path(__file__).parent

    def workspace_directory(self, workspace_id: str) -> _ExistingFileDirectory:
        UUID(workspace_id)
        return _ExistingFileDirectory()

    @contextmanager
    def _connect(self, _path: Path):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE preflight_decision (
                    run_id VARCHAR,
                    ordinal BIGINT,
                    dataset VARCHAR,
                    status VARCHAR,
                    decision_json VARCHAR
                )
                """
            )
            decisions = (
                ("ready", "CREATE"),
                ("ready", "UPDATE"),
                ("ready", "UNCHANGED"),
                ("needs_review", "AMBIGUOUS"),
                ("blocked", "BLOCKED"),
            )
            rows = []
            for ordinal, (status, classification) in enumerate(
                decisions,
                start=1,
            ):
                rows.append(
                    [
                        RUN_ID,
                        ordinal,
                        "contacts",
                        status,
                        json.dumps(
                            {
                                "dataset": "contacts",
                                "dataset_label": "Contacts",
                                "source_row": ordinal,
                                "status": status,
                                "classification": classification,
                                "identity": f"ROW-{ordinal}",
                                "reason": "Test row",
                                "field": "",
                                "recommended_action": "Review it",
                                "technical_code": "TEST",
                            }
                        ),
                    ]
                )
            connection.executemany(
                "INSERT INTO preflight_decision VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _ensure_workspace_database_schema(_connection) -> None:
        return None


class PreflightRepositoryReadinessRowTests(unittest.TestCase):
    def test_card_filters_select_their_matching_rows(self) -> None:
        repository = PreflightRepository(
            _InMemoryDatabase(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
        page = repository.get_readiness_rows(
            WORKSPACE_ID,
            RUN_ID,
            status="attention",
            page_size=20,
        )

        self.assertEqual(page.matching_count, 2)
        self.assertEqual(
            tuple(item.status for item in page.items),
            ("needs_review", "blocked"),
        )

        for expected_ordinal, status in enumerate(
            ("create", "update", "unchanged"),
            start=1,
        ):
            with self.subTest(status=status):
                outcome_page = repository.get_readiness_rows(
                    WORKSPACE_ID,
                    RUN_ID,
                    status=status,
                    page_size=20,
                )
                self.assertEqual(outcome_page.matching_count, 1)
                self.assertEqual(
                    outcome_page.items[0].identity,
                    f"ROW-{expected_ordinal}",
                )


class DeferredScopeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            dir=REPOSITORY_ROOT / ".tmp"
        )
        database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspaces = WorkspaceStateRepository(database)
        self.repository = PreflightRepository(database, self.workspaces)
        self.workspace = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Deferred scope persistence",
            source_system="CSV",
        )
        self.workspaces.initialize_workbench(self.workspace, actor=LOCAL_ACTOR)
        self.run_id = str(uuid4())
        path = self.repository.workspace_directory(self.workspace.workspace_id) / (
            "workspace-engine.duckdb"
        )
        with self.repository._connect(path) as connection:
            connection.execute(
                "INSERT INTO preflight_current VALUES (1, ?)",
                [self.run_id],
            )
            connection.execute(
                """
                INSERT INTO preflight_execution_projection VALUES (
                    ?, ?, ?, 'BLOCKED', 4, 1, 0, 1, 0, 0,
                    ?, '19.0', '', '', '', '', FALSE, 1
                )
                """,
                [
                    self.run_id,
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    "sha256:" + "3" * 64,
                ],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_acceptance_atomically_replaces_compact_load_projection(self) -> None:
        summary = ExecutionPreviewSummary(
            preflight_run_id=self.run_id,
            snapshot_hash="sha256:" + "4" * 64,
            snapshot_root_hash="sha256:" + "5" * 64,
            comparison_status="READY",
            create_count=2,
            update_count=1,
            unchanged_count=0,
            blocked_count=0,
            ambiguous_count=0,
            relationship_blocker_count=0,
            target_hash="sha256:" + "3" * 64,
            target_odoo_version="19.0",
            read_credential_binding_hash="",
            read_principal_hash="",
            read_permission_hash="",
            read_context_hash="",
            execution_shape_ready=True,
        )

        self.repository.save_deferred_scope_projection(
            self.workspace.workspace_id,
            self.run_id,
            execution_summary=summary,
            decision_hash="sha256:" + "6" * 64,
            actor=LOCAL_ACTOR,
        )

        path = self.repository.workspace_directory(self.workspace.workspace_id) / (
            "workspace-engine.duckdb"
        )
        with self.repository._connect(path) as connection:
            projection = connection.execute(
                """
                SELECT snapshot_hash, comparison_status, create_count,
                       update_count, blocked_count, execution_shape_ready
                  FROM preflight_execution_projection
                 WHERE run_id = ?
                """,
                [self.run_id],
            ).fetchone()
            transition = connection.execute(
                """
                SELECT event_type, detail FROM preflight_transition
                 WHERE run_id = ?
                """,
                [self.run_id],
            ).fetchone()
            approval = connection.execute(
                "SELECT approval_status FROM workspace_projection_cache"
            ).fetchone()
        self.assertEqual(
            projection,
            (summary.snapshot_hash, "READY", 2, 1, 0, True),
        )
        self.assertEqual(
            transition,
            ("DEFERRED_SCOPE_ACCEPTED", "sha256:" + "6" * 64),
        )
        self.assertEqual(approval, ("APPROVED",))


if __name__ == "__main__":
    unittest.main()
