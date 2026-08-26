from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.execution_repository import ExecutionRepository
from impodo.adapters.duckdb.reconciliation_repository import ReconciliationRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.workspace.workbench import WorkspaceState


ROOT = Path(__file__).resolve().parents[1]
HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64


class ExecutionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(self.database)
        self.repository = ExecutionRepository(self.database)
        self.reconciliation = ReconciliationRepository(self.database)
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Execution journal",
            source_system="CSV",
        )
        self.workspace_states.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
        self.preflight_id = str(uuid4())
        path = self.workspace_states.workspace_directory(self.workspace_state.workspace_id) / "workspace-engine.duckdb"
        with self.workspace_states._connect(path) as connection:
            connection.execute(
                """
                INSERT INTO readiness_run (
                    run_id, mapping_id, mapping_version, mapping_content_hash,
                    target_hash, staging_run_id, staging_content_hash,
                    quality_run_id, quality_content_hash, checked_at,
                    checked_by, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self.preflight_id,
                    str(uuid4()),
                    1,
                    HASH,
                    TARGET_HASH,
                    str(uuid4()),
                    HASH,
                    str(uuid4()),
                    HASH,
                    datetime.now(timezone.utc).isoformat(),
                    "Test operator",
                    "{}",
                ],
            )
            connection.execute(
                "INSERT INTO preflight_current VALUES (1, ?)",
                [self.preflight_id],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self) -> ExecutionRun:
        rows = tuple(
            ExecutionRowAttempt(
                row_id="sha256:" + f"{index:064x}",
                dataset="contacts",
                source_row=index + 2,
                target_model="res.partner",
                operation="CREATE" if index == 1 else "UPDATE",
                field_names=("name",),
                proposed_external_id=f"impodo_test.contact_{index}",
            )
            for index in (1, 2)
        )
        return ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=self.workspace_state.workspace_id,
            snapshot_hash=HASH,
            snapshot_root_hash=HASH,
            preflight_run_id=self.preflight_id,
            target_hash=TARGET_HASH,
            target_database="odoo19_disposable",
            batch_rows=10,
            status=ExecutionRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            started_by="Test operator",
            completed_at=None,
            rows=rows,
        )

    def test_journals_every_row_and_reloads_terminal_result(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (
                replace(
                    run.rows[0],
                    status=ExecutionRowStatus.COMMITTED,
                    attempt=1,
                    odoo_id=42,
                ),
                replace(
                    run.rows[1],
                    status=ExecutionRowStatus.BLOCKED,
                    safe_error="Dependency did not complete",
                ),
            ),
        )

        finished = self.repository.finish_run(
            self.workspace_state.workspace_id,
            run.run_id,
            ExecutionRunStatus.COMPLETED_WITH_ERRORS,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(finished.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(finished.committed_count, 1)
        self.assertEqual(finished.blocked_count, 1)
        self.assertEqual(
            self.repository.get_current_run(
                self.workspace_state.workspace_id,
                HASH,
            ),
            finished,
        )
        self.assertEqual(finished.batch_rows, 10)
        path = self.workspace_states.workspace_directory(self.workspace_state.workspace_id) / "workspace-engine.duckdb"
        with self.workspace_states._connect(path) as connection:
            events = connection.execute(
                """
                SELECT event_type FROM audit_event
                 WHERE event_type LIKE 'ODOO_LOAD_%' ORDER BY event_id
                """
            ).fetchall()
        self.assertEqual(events, [("ODOO_LOAD_STARTED",), ("ODOO_LOAD_FINISHED",)])

    def test_created_row_can_progress_from_partial_to_committed(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        partial = replace(
            run.rows[0],
            status=ExecutionRowStatus.PARTIALLY_APPLIED,
            attempt=1,
            odoo_id=42,
            safe_error="Created; deferred relationship update pending",
        )
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (partial,),
        )
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (
                replace(
                    partial,
                    status=ExecutionRowStatus.COMMITTED,
                    safe_error="",
                ),
                replace(
                    run.rows[1],
                    status=ExecutionRowStatus.BLOCKED,
                    safe_error="Not part of this test",
                ),
            ),
        )

        finished = self.repository.finish_run(
            self.workspace_state.workspace_id,
            run.run_id,
            ExecutionRunStatus.COMPLETED_WITH_ERRORS,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(finished.rows[0].status, ExecutionRowStatus.COMMITTED)
        self.assertEqual(finished.rows[0].odoo_id, 42)



    def test_publishes_one_hash_bound_readback_result(self) -> None:
        run = self._run()
        self.repository.start_run(self.workspace_state.workspace_id, run, actor=LOCAL_ACTOR)
        committed = tuple(
            replace(
                row,
                status=ExecutionRowStatus.COMMITTED,
                attempt=1,
                odoo_id=40 + index,
            )
            for index, row in enumerate(run.rows)
        )
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            committed,
        )
        self.repository.finish_run(
            self.workspace_state.workspace_id,
            run.run_id,
            ExecutionRunStatus.COMPLETED,
            actor=LOCAL_ACTOR,
        )
        report = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=self.workspace_state.workspace_id,
            execution_run_id=run.run_id,
            snapshot_hash=run.snapshot_hash,
            target_hash=run.target_hash,
            target_database=run.target_database,
            status=ReconciliationRunStatus.VERIFIED,
            verified_at=datetime.now(timezone.utc),
            verified_by="Test operator",
            unchanged_count=1,
            rows=tuple(
                ReconciliationRow(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    target_model=row.target_model,
                    operation=row.operation,
                    execution_status="COMMITTED",
                    status=ReconciliationRowStatus.VERIFIED,
                    odoo_id=row.odoo_id,
                    message="Odoo matches the confirmed load preview",
                )
                for row in committed
            ),
        )

        self.reconciliation.publish(
            self.workspace_state.workspace_id,
            report,
            actor=LOCAL_ACTOR,
        )

        restored = self.reconciliation.get_current(
            self.workspace_state.workspace_id,
            run.run_id,
        )
        self.assertEqual(restored.semantic_hash, report.semantic_hash)
        self.assertEqual(restored.verified_count, 3)



if __name__ == "__main__":
    unittest.main()

