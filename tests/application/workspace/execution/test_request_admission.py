"""Preserve accepted receipts when a later request cannot be admitted."""

from dataclasses import replace
from datetime import UTC, datetime
from itertools import product
from uuid import uuid4
import unittest

from impodo.domain.execution.odoo_scope import execution_api_scope
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_write import OdooRequestDeferred
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as fixtures


class _DeferredWriter(fixtures._Executor):
    def __init__(self, scope, *, after):
        super().__init__(scope)
        self.admissions = 0
        self.after = after

    def admit_write(self):
        self.admissions += 1
        if self.admissions > self.after:
            raise OdooRequestDeferred("Synthetic cooldown; request not sent")


class RequestAdmissionTests(unittest.TestCase):
    def test_later_create_and_update_remain_unattempted_and_resume_after_readback(self):
        for major, mode, accepted in product(
            (19, 20), tuple(OdooConnectionMode), (0, 1, 2)
        ):
            with self.subTest(major=major, mode=mode, accepted=accepted):
                snapshot = replace(
                    fixtures._snapshot(),
                    target_odoo_version=f"{major}.0",
                    target_module_versions={"base": f"{major}.0.1.0"},
                )
                service, journal = fixtures.ExecutionServiceTests()._service(
                    snapshot, mode=mode
                )
                scope = execution_api_scope(snapshot).semantic_hash
                writer = _DeferredWriter(scope, after=accepted)
                run = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=writer,
                    actor=LOCAL_ACTOR,
                )
                self.assertEqual(run.status, ExecutionRunStatus.RUNNING)
                self.assertEqual(run.committed_count, accepted)
                self.assertEqual(run.in_flight_count, 0)
                self.assertEqual(run.unknown_count, 0)
                self.assertEqual(run.blocked_count, 0)
                self.assertEqual(run.planned_count, 3 - accepted)
                self.assertTrue(
                    all(
                        row.attempt == 0
                        for row in run.rows
                        if row.status is ExecutionRowStatus.PLANNED
                    )
                )
                recovery = ReconciliationRun(
                    reconciliation_id=str(uuid4()),
                    workspace_id=snapshot.workspace_id,
                    execution_run_id=run.run_id,
                    snapshot_hash=snapshot.semantic_hash,
                    target_hash=snapshot.target_hash,
                    target_database=snapshot.target_database,
                    status=ReconciliationRunStatus.FALLOUT,
                    verified_at=datetime.now(UTC),
                    verified_by="Synthetic operator",
                    unchanged_count=0,
                    rows=tuple(
                        ReconciliationRow(
                            row_id=row.row_id,
                            dataset=row.dataset,
                            source_row=row.source_row,
                            target_model=row.target_model,
                            operation=row.operation,
                            execution_status=row.status.value,
                            status=ReconciliationRowStatus.VERIFIED
                            if row.status is ExecutionRowStatus.COMMITTED
                            else ReconciliationRowStatus.NOT_WRITTEN,
                            odoo_id=row.odoo_id,
                            message="Synthetic exact read-back",
                        )
                        for row in run.rows
                    ),
                )
                resumed_writer = fixtures._Executor(scope)
                completed = service.resume(
                    snapshot.workspace_id,
                    expected_execution_run_id=run.run_id,
                    recovery=recovery,
                    executor=resumed_writer,
                    actor=LOCAL_ACTOR,
                )
                self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED)
                self.assertEqual(completed.committed_count, 3)
                self.assertTrue(all(row.attempt == 1 for row in journal.run.rows))
                self.assertEqual(
                    len(resumed_writer.creates) + len(resumed_writer.loads),
                    max(0, 2 - accepted),
                )
