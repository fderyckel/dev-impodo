from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.execution_repository import ExecutionRepository
from tests.support.business_intent_assessment import seed_journal_assessment
from impodo.adapters.duckdb.reconciliation_repository import ReconciliationRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
    ProjectedOdooReceipt,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    UncertainWriteReason,
    WriteFailureCode,
)
from impodo.domain.execution.operator_errors import capture_operator_error, OperatorErrorPersistenceError
from tests.support.database_probe import count_duckdb_statements
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState
from impodo.application.transfer_preflight_service import TransferPreflightService
from tests.application.workspace.test_transfer_preflight import (
    _approved_state,
    _fresh,
)
from impodo.domain.reconciliation_detail import ReconciliationDetailManifest


ROOT = REPOSITORY_ROOT
HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64


class _AssessedExecutionRepository(ExecutionRepository):
    """Keep row-journal tests independent from comparison and intent authoring."""

    def start_run(self, workspace_id, run, *, actor, **kwargs):
        if not kwargs.get("correction_plan_hash") and not kwargs.get("transfer_preflight_hash"):
            path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
            with self._connect(path) as connection:
                kwargs["intent_assessment_hash"] = seed_journal_assessment(connection, run, actor=actor)
        return super().start_run(workspace_id, run, actor=actor, **kwargs)


class ExecutionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(self.database)
        self.repository = _AssessedExecutionRepository(self.database)
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

    def test_rejection_diagnostic_survives_repository_reopen(self) -> None:
        run = self._run()
        self.repository.start_run(self.workspace_state.workspace_id, run, actor=LOCAL_ACTOR)
        started = self._start_batch(run, (run.rows[0],), phase="CREATE", batch=0)[0]
        diagnostic = OdooWriteDiagnostic(WriteFailureCode.VALIDATION, "res.partner", "load", fields=("name",))
        failed = replace(started, status=ExecutionRowStatus.FAILED, safe_error=diagnostic.summary, diagnostic=diagnostic)
        self.repository.record_outcomes(self.workspace_state.workspace_id, run.run_id, (failed,))
        reopened = ExecutionRepository(self.database).get_run(self.workspace_state.workspace_id, run.run_id)
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.rows[0], failed)

    def test_uncertain_receipt_diagnostic_survives_repository_reopen(self):
        run = self._run()
        workspace = self.workspace_state.workspace_id
        self.repository.start_run(workspace, run, actor=LOCAL_ACTOR)
        started = self._start_batch(run, (run.rows[0],), phase="CREATE", batch=0)[0]
        diagnostic = OdooWriteDiagnostic(
            WriteFailureCode.UNKNOWN, "res.partner", "load", batch_size=1, http_status=200,
            uncertainty_reason=UncertainWriteReason.RECEIPT_COUNT_MISMATCH, returned_id_count=0,
        )
        unknown = replace(
            started, status=ExecutionRowStatus.OUTCOME_UNKNOWN,
            safe_error=diagnostic.summary, diagnostic=diagnostic,
        )
        self.repository.record_outcomes(workspace, run.run_id, (unknown,))
        reopened = ExecutionRepository(self.database).get_run(workspace, run.run_id)
        self.assertEqual(reopened.rows[0], unknown)

    def test_private_rejection_survives_reopen_without_entering_public_projection(self):
        run = self._run()
        workspace = self.workspace_state.workspace_id
        self.repository.start_run(workspace, run, actor=LOCAL_ACTOR)
        started = self._start_batch(run, (run.rows[0],), phase="CREATE", batch=0)[0]
        detail = capture_operator_error(
            [{"type": "error", "message": "Fictional rule rejected wire-value", "field": "name"}],
            ({"name": "wire-value"},),
        )
        diagnostic = OdooWriteDiagnostic(WriteFailureCode.VALIDATION, "res.partner", "load", operator_detail_status="AVAILABLE")
        failed = replace(started, status=ExecutionRowStatus.FAILED, safe_error=diagnostic.summary, diagnostic=diagnostic)
        self.repository.record_outcomes_with_detail(workspace, run.run_id, (failed,), detail)
        reopened = ExecutionRepository(self.database)
        saved_row, receipt = reopened.operator_error_for_row(workspace, run.run_id, failed.row_id)
        self.assertEqual(saved_row, failed)
        self.assertEqual(receipt.snapshot_hash, run.snapshot_hash)
        self.assertEqual(receipt.row_ids, (failed.row_id,))
        self.assertEqual(receipt.detail, detail)
        self.assertNotIn("wire-value", "\n".join(row.to_json() for row in reopened.get_run(workspace, run.run_id).rows))
        path = reopened.workspace_directory(workspace) / "workspace-engine.duckdb"
        with reopened._connect(path) as connection:
            connection.execute("UPDATE execution_operator_error SET detail_hash = 'changed'")
        self.assertEqual(reopened.operator_error_for_row(workspace, run.run_id, failed.row_id), (failed, None))
        self.assertIsNone(reopened.operator_error_for_row(workspace, run.run_id, "other-row"))

    def test_private_capture_write_failure_preserves_known_outcome_and_other_receipts(self):
        run = self._run()
        workspace = self.workspace_state.workspace_id
        self.repository.start_run(workspace, run, actor=LOCAL_ACTOR)
        started = self._start_batch(run, (run.rows[0],), phase="CREATE", batch=0)[0]
        diagnostic = OdooWriteDiagnostic(WriteFailureCode.VALIDATION, "res.partner", "load", operator_detail_status="AVAILABLE")
        failed = replace(started, status=ExecutionRowStatus.FAILED, safe_error=diagnostic.summary, diagnostic=diagnostic)
        detail = capture_operator_error([{"type": "error", "message": "Fictional rule"}], ({"name": "private-value"},))
        with patch("impodo.adapters.duckdb.execution_repository.write_operator_error", side_effect=OperatorErrorPersistenceError("injected")):
            self.repository.record_outcomes_with_detail(workspace, run.run_id, (failed,), detail)
        reopened = ExecutionRepository(self.database)
        saved, receipt = reopened.operator_error_for_row(workspace, run.run_id, failed.row_id)
        self.assertEqual(saved.status, ExecutionRowStatus.FAILED)
        self.assertEqual(saved.diagnostic.operator_detail_status, "CAPTURE_FAILED")
        self.assertIsNone(receipt)
        self.assertEqual(reopened.get_run(workspace, run.run_id).rows[1], run.rows[1])

    def test_rejected_row_navigation_is_bounded_and_keeps_exact_saved_positions(self):
        run = self._run()
        run = replace(run, rows=tuple(replace(run.rows[0], row_id="sha256:" + f"{i:064x}", source_row=i + 90) for i in range(45)))
        workspace = self.workspace_state.workspace_id
        self.repository.start_run(workspace, run, actor=LOCAL_ACTOR)
        started = self._start_batch(run, run.rows, phase="CREATE", batch=0)
        failed = tuple(replace(row, status=ExecutionRowStatus.FAILED) for row in started)
        self.repository.record_outcomes(workspace, run.run_id, failed)
        with count_duckdb_statements() as statements:
            saved_run, page = self.repository.operator_error_rows(workspace, None, after=-1)
        self.assertEqual(saved_run, run.run_id)
        self.assertEqual(len(page), 21)
        self.assertEqual(page[0][1].source_row, 90)
        self.assertEqual(len(statements), 2)
        self.assertIn("LIMIT 21", statements[-1])
        _, next_page = self.repository.operator_error_rows(workspace, run.run_id, after=page[19][0])
        self.assertEqual(next_page[0][1].source_row, 110)
        with count_duckdb_statements() as statements:
            self.assertEqual(self.repository.operator_error_for_row(workspace, run.run_id, failed[0].row_id), (failed[0], None))
        self.assertEqual(len(statements), 1)
        self.assertIsNone(self.repository.operator_error_rows(workspace, str(uuid4()), after=-1))

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

    def _fresh_recomparison(self) -> str:
        preflight_id = str(uuid4())
        path = self.repository.workspace_directory(self.workspace_state.workspace_id) / "workspace-engine.duckdb"
        with self.repository._connect(path) as connection:
            connection.execute("""
                INSERT INTO readiness_run (
                    run_id, mapping_id, mapping_version, mapping_content_hash,
                    target_hash, staging_run_id, staging_content_hash,
                    quality_run_id, quality_content_hash, checked_at, checked_by, report_json
                )
                SELECT ?, mapping_id, mapping_version, mapping_content_hash,
                       target_hash, staging_run_id, staging_content_hash,
                       quality_run_id, quality_content_hash, checked_at,
                       checked_by, report_json
                  FROM readiness_run WHERE run_id = ?
            """, [preflight_id, self.preflight_id])
            connection.execute("UPDATE preflight_current SET run_id = ?", [preflight_id])
        return preflight_id

    def test_close_for_recomparison_preserves_rows_and_prevents_old_worker_writes(self) -> None:
        run = self._run()
        self.repository.start_run(self.workspace_state.workspace_id, run, actor=LOCAL_ACTOR)
        started = self._start_batch(run, (run.rows[0],), phase="CREATE", batch=0)
        self.repository.record_outcomes(self.workspace_state.workspace_id, run.run_id,
            (replace(started[0], status=ExecutionRowStatus.COMMITTED, odoo_id=71),))
        expected = self.repository.get_run(self.workspace_state.workspace_id, run.run_id)
        fresh = self._fresh_recomparison()
        closed = self.repository.close_interrupted_run_for_recomparison(
            self.workspace_state.workspace_id, expected, fresh, actor=LOCAL_ACTOR)
        self.assertEqual(closed.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(closed.rows, expected.rows)
        self.assertIsNotNone(closed.completed_at)
        with self.assertRaisesRegex(WorkspaceError, "not active"):
            self._start_batch(run, (run.rows[1],), phase="UPDATE", batch=1)
        new = replace(self._run(), preflight_run_id=fresh, snapshot_hash="sha256:" + "3" * 64)
        self.repository.start_run(self.workspace_state.workspace_id, new, actor=LOCAL_ACTOR)
        self.assertEqual(self.repository.get_run(self.workspace_state.workspace_id, run.run_id).rows, expected.rows)

    def test_close_for_recomparison_rejects_rows_changed_after_assessment(self) -> None:
        run = self._run()
        self.repository.start_run(self.workspace_state.workspace_id, run, actor=LOCAL_ACTOR)
        fresh = self._fresh_recomparison()
        self._start_batch(run, (run.rows[0],), phase="CREATE", batch=0)
        with self.assertRaisesRegex(WorkspaceError, "changed before"):
            self.repository.close_interrupted_run_for_recomparison(
                self.workspace_state.workspace_id, run, fresh, actor=LOCAL_ACTOR)
        self.assertEqual(self.repository.get_run(self.workspace_state.workspace_id, run.run_id).status,
                         ExecutionRunStatus.RUNNING)

    def test_close_for_recomparison_requires_a_current_different_comparison(self) -> None:
        run = self._run()
        self.repository.start_run(self.workspace_state.workspace_id, run, actor=LOCAL_ACTOR)
        for preflight_id in (self.preflight_id, str(uuid4())):
            with self.subTest(preflight_id=preflight_id), self.assertRaisesRegex(WorkspaceError, "changed before"):
                self.repository.close_interrupted_run_for_recomparison(
                    self.workspace_state.workspace_id, run, preflight_id, actor=LOCAL_ACTOR)

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
        self.assertIsNone(finished.rows[0].diagnostic)
        path = self.workspace_states.workspace_directory(self.workspace_state.workspace_id) / "workspace-engine.duckdb"
        with self.workspace_states._connect(path) as connection:
            events = connection.execute(
                """
                SELECT event_type FROM audit_event
                 WHERE event_type LIKE 'ODOO_LOAD_%' ORDER BY event_id
                """
            ).fetchall()
        self.assertEqual(events, [("ODOO_LOAD_STARTED",), ("ODOO_LOAD_FINISHED",)])

    def test_hot_write_transitions_validate_full_schema_once(self) -> None:
        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
        )
        original = self.repository._ensure_workspace_database_schema

        with patch.object(
            self.repository,
            "_ensure_workspace_database_schema",
            wraps=original,
        ) as validate_schema:
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
                        odoo_id=42,
                    ),
                ),
            )

        self.assertEqual(validate_schema.call_count, 1)

    def test_cached_schema_does_not_bypass_closed_workspace_guard(self) -> None:
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
        path = (
            self.workspace_states.workspace_directory(
                self.workspace_state.workspace_id
            )
            / "workspace-engine.duckdb"
        )
        with self.workspace_states._connect(path) as connection:
            connection.execute(
                "UPDATE workspace_projection_cache SET status = 'CLOSED' "
                "WHERE singleton_id = 1"
            )

        with self.assertRaisesRegex(WorkspaceError, "closed and read-only"):
            self.repository.record_outcomes(
                self.workspace_state.workspace_id,
                run.run_id,
                (
                    replace(
                        create,
                        status=ExecutionRowStatus.COMMITTED,
                        odoo_id=42,
                    ),
                ),
            )

    def test_correction_run_uses_exact_update_ids_without_preflight_pointer(self):
        run = self._run()
        correction = replace(
            run,
            preflight_run_id=str(uuid4()),
            rows=tuple(
                replace(
                    row,
                    operation="UPDATE",
                    odoo_id=100 + index,
                    proposed_external_id="",
                )
                for index, row in enumerate(run.rows)
            ),
        )

        self.repository.start_run(
            self.workspace_state.workspace_id,
            correction,
            actor=LOCAL_ACTOR,
            correction_plan_hash=HASH,
        )

        stored = self.repository.get_current_run(self.workspace_state.workspace_id)
        self.assertEqual(stored, correction)
        path = self.workspace_states.workspace_directory(
            self.workspace_state.workspace_id
        ) / "workspace-engine.duckdb"
        with self.workspace_states._connect(path) as connection:
            events = connection.execute(
                "SELECT event_type FROM audit_event ORDER BY event_id"
            ).fetchall()
        self.assertEqual(events[-1], ("ODOO_CORRECTION_STARTED",))

    def test_transfer_run_requires_current_hash_bound_preflight(self):
        workspace, package, approval, match = _approved_state()
        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match),
            recorded_by=LOCAL_ACTOR.identity,
        )
        report = replace(
            report,
            workspace_id=self.workspace_state.workspace_id,
            destination_target_hash=TARGET_HASH,
        )
        path = self.workspace_states.workspace_directory(
            self.workspace_state.workspace_id
        ) / "workspace-engine.duckdb"
        with self.workspace_states._connect(path) as connection:
            connection.execute(
                """
                UPDATE workspace_projection_cache
                   SET transfer_preflight_report_json = ?
                 WHERE singleton_id = 1
                """,
                [report.to_json()],
            )

        run = self._run()
        self.repository.start_run(
            self.workspace_state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
            transfer_preflight_hash=report.content_hash,
        )

        self.assertEqual(
            self.repository.get_current_run(self.workspace_state.workspace_id),
            run,
        )
        with self.workspace_states._connect(path) as connection:
            events = connection.execute(
                "SELECT event_type FROM audit_event ORDER BY event_id"
            ).fetchall()
        self.assertEqual(events[-1], ("ODOO_TRANSFER_LOAD_STARTED",))

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



    def test_appends_hash_bound_readback_results_and_keeps_latest_current(self) -> None:
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

        with self.assertRaisesRegex(WorkspaceError, "cannot be published"):
            self.reconciliation.publish(
                self.workspace_state.workspace_id,
                replace(report, readback_scope="RECOVERY_TARGETED"),
                actor=LOCAL_ACTOR,
            )
        with self.assertRaisesRegex(WorkspaceError, "does not cover every"):
            self.reconciliation.publish(
                self.workspace_state.workspace_id,
                replace(report, rows=report.rows[:1]),
                actor=LOCAL_ACTOR,
            )

        manifest = ReconciliationDetailManifest(
            reconciliation_id=report.reconciliation_id,
            storage_name="fallout-detail.json",
            logical_hash="sha256:" + "3" * 64,
            artifact_hash="sha256:" + "4" * 64,
            size_bytes=123,
            difference_count=0,
        )
        self.reconciliation.publish(
            self.workspace_state.workspace_id,
            report,
            actor=LOCAL_ACTOR,
            detail=manifest,
        )

        second = replace(
            report,
            reconciliation_id=str(uuid4()),
            verified_at=report.verified_at + timedelta(seconds=1),
        )
        self.reconciliation.publish(
            self.workspace_state.workspace_id,
            second,
            actor=LOCAL_ACTOR,
        )

        restored = self.reconciliation.get_current(
            self.workspace_state.workspace_id,
            run.run_id,
        )
        self.assertEqual(restored.semantic_hash, second.semantic_hash)
        self.assertEqual(restored.verified_count, 3)
        self.assertEqual(
            self.reconciliation.history(
                self.workspace_state.workspace_id,
                run.run_id,
            ),
            (report, second),
        )
        self.assertEqual(
            self.reconciliation.get_detail_manifest(
                self.workspace_state.workspace_id,
                report.reconciliation_id,
            ),
            manifest,
        )
        self.assertIsNone(
            self.reconciliation.get_detail_manifest(
                self.workspace_state.workspace_id,
                second.reconciliation_id,
            )
        )



if __name__ == "__main__":
    unittest.main()
