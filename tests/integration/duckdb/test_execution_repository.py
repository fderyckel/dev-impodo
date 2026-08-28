from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from dataclasses import replace
from datetime import datetime, timezone
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
    ProjectedOdooReceipt,
)
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState


ROOT = REPOSITORY_ROOT
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
                schedule_component=0,
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

    def _start_batch(
        self,
        run: ExecutionRun,
        rows: tuple[ExecutionRowAttempt, ...],
        *,
        phase: str,
        batch: int,
    ) -> tuple[ExecutionRowAttempt, ...]:
        started = tuple(
            replace(
                row,
                status=ExecutionRowStatus.IN_FLIGHT,
                attempt=row.attempt + 1,
                transport_page=0,
                transport_batch=batch,
                transport_phase=phase,
                odoo_id=(row.odoo_id or (42 if phase == "UPDATE" else None)),
                safe_error="",
            )
            for row in rows
        )
        self.repository.record_batch_started(
            self.workspace_state.workspace_id,
            run.run_id,
            started,
        )
        return started

    def test_journals_every_row_and_reloads_terminal_result(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        create = self._start_batch(
            run,
            (run.rows[0],),
            phase="CREATE",
            batch=0,
        )[0]
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (
                replace(
                    create,
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
        create = self._start_batch(
            run,
            (run.rows[0],),
            phase="CREATE",
            batch=0,
        )[0]
        partial = replace(
            create,
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
        completion = self._start_batch(
            run,
            (partial,),
            phase="COMPLETION",
            batch=1,
        )[0]
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (
                replace(
                    completion,
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

    def test_projected_receipt_is_durable_and_cannot_change(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        create = self._start_batch(
            run,
            (run.rows[0],),
            phase="CREATE",
            batch=0,
        )[0]
        partial = replace(
            create,
            status=ExecutionRowStatus.PARTIALLY_APPLIED,
            odoo_id=42,
            safe_error="Created; generated relationship read-back pending",
        )
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (partial,),
        )
        receipt = ProjectedOdooReceipt(
            projection_field="product_variant_id",
            target_model="product.product",
            odoo_id=142,
        )
        projected = replace(
            partial,
            projected_receipts=(receipt,),
        )
        self.repository.record_outcomes(
            self.workspace_state.workspace_id,
            run.run_id,
            (projected,),
        )

        reloaded = self.repository.get_run(
            self.workspace_state.workspace_id,
            run.run_id,
        )

        self.assertEqual(reloaded.rows[0].projected_receipts, (receipt,))
        with self.assertRaisesRegex(WorkspaceError, "projected.*changed"):
            self.repository.record_outcomes(
                self.workspace_state.workspace_id,
                run.run_id,
                (
                    replace(
                        projected,
                        status=ExecutionRowStatus.COMMITTED,
                        safe_error="",
                        projected_receipts=(replace(receipt, odoo_id=999),),
                    ),
                ),
            )

    def test_process_restart_reloads_the_exact_in_flight_batch(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        started = self._start_batch(
            run,
            (run.rows[0],),
            phase="CREATE",
            batch=7,
        )[0]

        restarted_repository = ExecutionRepository(self.database)
        reloaded = restarted_repository.get_current_run(
            self.workspace_state.workspace_id,
            run.snapshot_hash,
        )

        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.status, ExecutionRunStatus.RUNNING)
        self.assertEqual(reloaded.in_flight_count, 1)
        self.assertEqual(reloaded.active_component, 0)
        self.assertEqual(reloaded.active_batch, (7,))
        self.assertEqual(reloaded.rows[0], started)
        with self.assertRaisesRegex(WorkspaceError, "unattempted rows"):
            restarted_repository.finish_run(
                self.workspace_state.workspace_id,
                run.run_id,
                ExecutionRunStatus.COMPLETED,
                actor=LOCAL_ACTOR,
            )

    def test_recovery_evidence_atomically_marks_a_safe_create_retry(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        started = self._start_batch(
            run,
            (run.rows[0],),
            phase="CREATE",
            batch=3,
        )[0]
        recovery_hash = "sha256:" + "9" * 64
        recovered = (
            replace(
                started,
                status=ExecutionRowStatus.RETRY_READY,
                safe_error="Read-back proved no create was applied",
                recovery_hash=recovery_hash,
            ),
            replace(run.rows[1], recovery_hash=recovery_hash),
        )

        self.repository.record_recovery(
            self.workspace_state.workspace_id,
            run.run_id,
            recovered,
            actor=LOCAL_ACTOR,
        )
        reloaded = self.repository.get_run(
            self.workspace_state.workspace_id,
            run.run_id,
        )

        self.assertEqual(reloaded.retry_ready_count, 1)
        self.assertEqual(
            {item.recovery_hash for item in reloaded.rows},
            {recovery_hash},
        )



    def test_publishes_one_hash_bound_readback_result(self) -> None:
        run = self._run()
        self.repository.start_run(self.workspace_state.workspace_id, run, actor=LOCAL_ACTOR)
        started = (
            *self._start_batch(
                run,
                (run.rows[0],),
                phase="CREATE",
                batch=0,
            ),
            *self._start_batch(
                run,
                (run.rows[1],),
                phase="UPDATE",
                batch=1,
            ),
        )
        committed = tuple(
            replace(
                row,
                status=ExecutionRowStatus.COMMITTED,
                odoo_id=row.odoo_id or 40 + index,
            )
            for index, row in enumerate(started)
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
