"""Exercise continuation with reviewed dependency plans for both supported majors."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4
import unittest

from impodo.domain.execution.load_control import (
    LoadControlState,
    RejectionPolicy,
    can_set_aside,
)
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.reconciliation import (
    ReconciliationRun,
    ReconciliationRow,
    ReconciliationRunStatus,
    ReconciliationRowStatus,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from impodo.domain.execution_snapshot import FieldIntent
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as fixtures


class _ControlledJournal(fixtures._Journal):
    def start_run(self, workspace_id, run, *, actor, load_control=None, **kwargs):
        super().start_run(
            workspace_id, replace(run, load_control=load_control), actor=actor, **kwargs
        )

    def get_load_control(self, workspace_id, run_id):
        return self.run.load_control

    def request_load_stop(self, workspace_id, run_id, *, actor):
        control = self.run.load_control.request_stop()
        self.run = replace(self.run, load_control=control)
        return control

    def finish_run(self, workspace_id, run_id, status, *, actor):
        result = super().finish_run(workspace_id, run_id, status, actor=actor)
        control = result.load_control
        state = (
            LoadControlState.STOPPED
            if control.state is LoadControlState.STOP_REQUESTED
            else LoadControlState.FINISHED
        )
        self.run = replace(result, load_control=replace(control, state=state))
        return self.run

    def resume_load_control(self, workspace_id, run_id, *, expected_hash, actor):
        if self.run.load_control.content_hash != expected_hash:
            raise AssertionError("stale control")
        control = self.run.load_control.continue_next_group()
        self.run = replace(self.run, load_control=control)
        return control

    def record_batch_started(self, workspace_id, run_id, rows):
        self.run.load_control.require_running()
        super().record_batch_started(workspace_id, run_id, rows)

    def record_outcomes(self, workspace_id, run_id, rows):
        control = self.run.load_control
        for item in rows:
            if self.rows[item.row_id].status is ExecutionRowStatus.IN_FLIGHT:
                if item.status in {
                    ExecutionRowStatus.FAILED,
                    ExecutionRowStatus.COMMITTED,
                }:
                    control = control.settle(
                        item.target_model,
                        item.transport_phase,
                        rejected=item.status is ExecutionRowStatus.FAILED,
                    )
        super().record_outcomes(workspace_id, run_id, rows)
        self.run = replace(self.run, load_control=control)

    def record_rejected_batch(self, workspace_id, run_id, rows, error):
        if not can_set_aside(error):
            raise AssertionError("unqualified split")
        retries = tuple(
            replace(row, status=ExecutionRowStatus.RETRY_READY) for row in rows
        )
        super().record_outcomes(workspace_id, run_id, retries)
        return retries


class _Writer(fixtures._Executor):
    def __init__(self, scope, bad_names):
        super().__init__(scope, lookup_ids=())
        self.bad_names = bad_names
        self.calls = 0

    def _reject(self, model, values):
        self.calls += 1
        if any(value.get("name") in self.bad_names for value in values):
            raise OdooWriteRejected(
                "safe",
                diagnostic=OdooWriteDiagnostic(
                    WriteFailureCode.VALIDATION, model, "load", batch_size=len(values)
                ),
            )

    def create_rows(self, model, values):
        self._reject(model, values)
        return super().create_rows(model, values)

    def load_create_rows(self, model, values, external_ids):
        self._reject(model, values)
        return super().load_create_rows(model, values, external_ids)


class ContinuationTests(unittest.TestCase):
    def _service(self, snapshot, mode=OdooConnectionMode.LOCAL):
        service, _ = fixtures.ExecutionServiceTests()._service(snapshot, mode=mode)
        journal = _ControlledJournal()
        service.journal = journal
        service.load_controls = journal
        return service, journal

    def test_failed_parent_blocks_child_but_independent_update_continues(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                if mode not in {OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE}:
                    continue
                with self.subTest(major=major, mode=mode):
                    snapshot = replace(
                        fixtures._snapshot(),
                        target_odoo_version=f"{major}.0",
                        target_module_versions={"base": f"{major}.0.1.0"},
                    )
                    service, journal = self._service(snapshot, mode)
                    writer = _Writer(
                        fixtures.execution_api_scope(snapshot).semantic_hash,
                        {"Category"},
                    )
                    writer.lookup_ids = (50,)
                    result = service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=writer,
                        actor=LOCAL_ACTOR,
                        rejection_policy=RejectionPolicy.CONTINUE,
                    )
                    self.assertEqual(
                        [row.status for row in result.rows],
                        [
                            ExecutionRowStatus.FAILED,
                            ExecutionRowStatus.BLOCKED,
                            ExecutionRowStatus.COMMITTED,
                        ],
                    )
                    self.assertEqual(len(writer.updates), 1)
                    self.assertEqual(
                        result.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS
                    )

    def test_stop_before_first_write_closes_pending_rows_without_transport(self):
        snapshot = fixtures._snapshot()
        service, journal = self._service(snapshot)
        writer = _Writer(fixtures.execution_api_scope(snapshot).semantic_hash, set())
        writer.lookup_ids = (50,)

        def stop(run):
            if run.status is ExecutionRunStatus.RUNNING:
                service.request_stop(
                    snapshot.workspace_id, run.run_id, actor=LOCAL_ACTOR
                )

        result = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=LOCAL_ACTOR,
            rejection_policy=RejectionPolicy.CONTINUE,
            progress=stop,
        )
        self.assertEqual(writer.calls, 0)
        self.assertEqual(result.load_control.state, LoadControlState.STOPPED)
        self.assertEqual(result.blocked_count, 3)

    def test_twenty_thousand_all_fail_pauses_after_ten_distinct_rows(self):
        template = fixtures._snapshot()
        rows = tuple(
            fixtures._row(
                dataset="categories",
                model="product.category",
                source_row=i + 2,
                source_identity=(f"{i:05d}",),
                business_identity=(f"{i:05d}",),
                disposition="CREATE",
                fields=(FieldIntent("name", "SET_VALUE", "bad"),),
            )
            for i in range(20_000)
        )
        snapshot = replace(
            template,
            rows=rows,
            datasets=(template.datasets[0],),
            counts={
                "CREATE": len(rows),
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        snapshot = fixtures._with_plan(snapshot)
        service, journal = self._service(snapshot)
        writer = _Writer(fixtures.execution_api_scope(snapshot).semantic_hash, {"bad"})
        result = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=LOCAL_ACTOR,
            rejection_policy=RejectionPolicy.CONTINUE,
        )
        self.assertEqual(result.load_control.state, LoadControlState.PAUSED)
        self.assertEqual(result.failed_count, 10)
        self.assertEqual(result.planned_count + result.retry_ready_count, 19_990)
        self.assertLess(writer.calls, 30)
        self.assertEqual(result.status, ExecutionRunStatus.RUNNING)

    def test_unknown_group_does_not_start_another_field_shape(self):
        template = fixtures._snapshot()
        rows = tuple(
            fixtures._row(
                dataset="categories",
                model="product.category",
                source_row=i + 2,
                source_identity=(str(i),),
                business_identity=(str(i),),
                disposition="CREATE",
                fields=(
                    FieldIntent("name", "SET_VALUE", str(i)),
                    *(
                        (FieldIntent("description", "SET_VALUE", "other fields"),)
                        if i
                        else ()
                    ),
                ),
            )
            for i in range(2)
        )
        snapshot = fixtures._with_plan(
            replace(
                template,
                rows=rows,
                datasets=(template.datasets[0],),
                counts={
                    "CREATE": 2,
                    "UPDATE": 0,
                    "UNCHANGED": 0,
                    "AMBIGUOUS": 0,
                    "BLOCKED": 0,
                },
            )
        )
        for policy in RejectionPolicy:
            with self.subTest(policy=policy):
                service, journal = self._service(snapshot)
                writer = _Writer(
                    fixtures.execution_api_scope(snapshot).semantic_hash, set()
                )
                writer.unknown = True
                result = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=writer,
                    actor=LOCAL_ACTOR,
                    rejection_policy=policy,
                )
                self.assertEqual(writer.calls, 1)
                self.assertEqual(result.unknown_count, 1)
                self.assertEqual(result.blocked_count, 1)
                self.assertEqual(result.status, ExecutionRunStatus.OUTCOME_UNKNOWN)

    def test_guarded_resume_keeps_rejected_rows_out_of_transport(self):
        template = fixtures._snapshot()
        rows = tuple(
            fixtures._row(
                dataset="categories",
                model="product.category",
                source_row=i + 2,
                source_identity=(f"{i:05d}",),
                business_identity=(f"{i:05d}",),
                disposition="CREATE",
                fields=(
                    FieldIntent("name", "SET_VALUE", "bad" if 10 <= i < 20 else "good"),
                ),
            )
            for i in range(200)
        )
        snapshot = fixtures._with_plan(
            replace(
                template,
                rows=rows,
                datasets=(template.datasets[0],),
                counts={
                    "CREATE": 200,
                    "UPDATE": 0,
                    "UNCHANGED": 0,
                    "AMBIGUOUS": 0,
                    "BLOCKED": 0,
                },
            )
        )
        service, journal = self._service(snapshot)
        writer = _Writer(fixtures.execution_api_scope(snapshot).semantic_hash, {"bad"})
        paused = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=LOCAL_ACTOR,
            rejection_policy=RejectionPolicy.CONTINUE,
            batch_rows=1_000,
        )
        self.assertEqual(paused.committed_count, 10)
        recovery = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=snapshot.workspace_id,
            execution_run_id=paused.run_id,
            snapshot_hash=snapshot.semantic_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=ReconciliationRunStatus.FALLOUT,
            verified_at=datetime.now(UTC),
            verified_by="Test operator",
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
                    message="Fixture read-back result",
                )
                for row in paused.rows
            ),
        )
        plan = service.review_recovery(
            snapshot.workspace_id,
            expected_execution_run_id=paused.run_id,
            recovery=recovery,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(plan.resume_count, 180)
        self.assertEqual(plan.load_control_hash, paused.load_control.content_hash)
        calls = writer.calls
        completed = service.resume(
            snapshot.workspace_id,
            expected_execution_run_id=paused.run_id,
            recovery=recovery,
            executor=writer,
            actor=LOCAL_ACTOR,
            expected_load_control_hash=plan.load_control_hash,
        )
        self.assertEqual(writer.calls - calls, 2)
        self.assertEqual(
            [len(values) for _model, values in writer.creates][-2:], [10, 170]
        )
        self.assertEqual(completed.failed_count, 10)
        self.assertEqual(completed.committed_count, 190)
        self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)

    def test_rejected_cycle_completion_keeps_its_peer_partial(self):
        for major in (19, 20):
            with self.subTest(major=major):
                names = ("first_nodes", "second_nodes", "third_nodes")
                rows = tuple(
                    fixtures._row(
                        dataset=name,
                        model="x.node",
                        source_row=20 + index,
                        source_identity=(name,),
                        business_identity=(name,),
                        disposition="CREATE",
                        fields=(
                            FieldIntent("code", "SET_VALUE", name),
                            *(
                                FieldIntent(
                                    f"link_{other_index}",
                                    "SET_VALUE",
                                    fixtures.LogicalReference(
                                        origin="incoming", key=(other,), dataset=other
                                    ),
                                    kind="relation",
                                    relation_operation="replace",
                                    related_model="x.node",
                                    related_identity_fields=("code",),
                                    dependency_strength="deferrable",
                                    defer_on_create=True,
                                )
                                for other_index, other in enumerate(names)
                                if other != name
                            ),
                        ),
                    )
                    for index, name in enumerate(names)
                )
                snapshot = fixtures._with_plan(
                    replace(
                        fixtures._snapshot(),
                        rows=rows,
                        datasets=tuple(
                            fixtures.ExecutionDataset(
                                name,
                                "x.node",
                                index,
                                tuple(other for other in names if other != name),
                                "update",
                                ("code",),
                                (),
                            )
                            for index, name in enumerate(names)
                        ),
                        counts={
                            "CREATE": 3,
                            "UPDATE": 0,
                            "UNCHANGED": 0,
                            "AMBIGUOUS": 0,
                            "BLOCKED": 0,
                        },
                        target_odoo_version=f"{major}.0",
                        target_module_versions={"base": f"{major}.0.1.0"},
                    )
                )
                service, journal = self._service(snapshot, OdooConnectionMode.REMOTE)
                writer = _Writer(
                    fixtures.execution_api_scope(snapshot).semantic_hash, set()
                )
                calls = []

                def reject(model, record_id, values):
                    calls.append((model, record_id))
                    raise OdooWriteRejected(
                        "safe",
                        diagnostic=OdooWriteDiagnostic(
                            WriteFailureCode.VALIDATION, model, "write"
                        ),
                    )

                writer.update_row = reject
                result = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=writer,
                    actor=LOCAL_ACTOR,
                    rejection_policy=RejectionPolicy.CONTINUE,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.partially_applied_count, 2)
                self.assertEqual(result.rejected_count, 1)
                self.assertTrue(
                    any("required record" in row.safe_error for row in result.rows)
                )
