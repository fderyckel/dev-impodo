"""Recovery requires exact evidence and never invents a safe retry."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
import unittest

from impodo.domain.execution.load_control import (
    LoadControl,
    LoadControlState,
    RejectionPolicy,
)
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
    ProjectedOdooReceipt,
)
from impodo.domain.execution.recovery import (
    classify_recovery,
    projected_receipt_requirements,
    targeted_recovery_row_ids,
)
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
)
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.workspace.errors import WorkspaceError

HASH = "sha256:" + "1" * 64
NOW = datetime(2026, 11, 17, tzinfo=UTC)


def _fixture(major="20"):
    row = ExecutionRow(
        row_id="parent",
        dataset="parents",
        source_row=1,
        source_trace_id=HASH,
        source_identity=("P1",),
        target_model="x.parent",
        business_identity=("P1",),
        business_scope=(),
        disposition="CREATE",
        target_match_count=0,
        proposed_external_id="impodo.parent",
        fields=(FieldIntent("name", "SET_VALUE", "P1"),),
    )
    snapshot = ExecutionSnapshot(
        workspace_id="workspace",
        preflight_run_id="preflight",
        mapping_id="mapping",
        mapping_version=1,
        mapping_content_hash=HASH,
        compiled_plan_hash=HASH,
        staging_run_id="staging",
        staging_content_hash=HASH,
        quality_run_id="quality",
        quality_content_hash=HASH,
        normalization_run_id="normalization",
        normalization_content_hash=HASH,
        normalization_lifecycle_version=1,
        eligible_dataset_hash=HASH,
        frozen_input_hash=HASH,
        preflight_result_hash=HASH,
        metadata_snapshot_hash=HASH,
        record_snapshot_hash=HASH,
        target_hash=HASH,
        target_database="target",
        target_odoo_version=major,
        target_snapshot_at=NOW.isoformat(),
        target_module_versions={},
        datasets=(
            ExecutionDataset("parents", "x.parent", 0, (), "reference", ("name",), ()),
        ),
        counts={"CREATE": 1},
        rows=(row,),
        root_hash=HASH,
    )
    attempt = ExecutionRowAttempt(
        row_id=row.row_id,
        dataset=row.dataset,
        source_row=row.source_row,
        target_model=row.target_model,
        operation=row.disposition,
        field_names=("name",),
        proposed_external_id=row.proposed_external_id,
        status=ExecutionRowStatus.IN_FLIGHT,
        attempt=1,
        schedule_component=0,
        transport_page=0,
        transport_batch=0,
        transport_phase="CREATE",
    )
    run = ExecutionRun(
        run_id="run",
        workspace_id=snapshot.workspace_id,
        snapshot_hash=snapshot.semantic_hash,
        snapshot_root_hash=HASH,
        preflight_run_id="preflight",
        target_hash=HASH,
        target_database="target",
        batch_rows=10,
        status=ExecutionRunStatus.RUNNING,
        started_at=NOW,
        started_by="operator",
        completed_at=None,
        rows=(attempt,),
    )
    report = ReconciliationRun(
        reconciliation_id="recovery",
        workspace_id=run.workspace_id,
        execution_run_id=run.run_id,
        snapshot_hash=run.snapshot_hash,
        target_hash=HASH,
        target_database="target",
        status=ReconciliationRunStatus.FALLOUT,
        verified_at=NOW,
        verified_by="operator",
        unchanged_count=0,
        rows=(
            ReconciliationRow(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                execution_status=attempt.status.value,
                status=ReconciliationRowStatus.NOT_APPLIED,
                retry_safe=True,
            ),
        ),
    )
    return snapshot, run, report


class RecoveryClassificationTests(unittest.TestCase):
    def test_absent_create_requires_proven_safe_retry_and_preserves_inputs(self):
        for major in ("19", "20"):
            with self.subTest(major=major):
                snapshot, run, report = _fixture(major)
                before = (snapshot.semantic_hash, asdict(run), report.to_json())
                recovered = classify_recovery(snapshot, run, report)
                self.assertEqual(recovered[0].status, ExecutionRowStatus.RETRY_READY)
                self.assertEqual(recovered[0].attempt, 1)
                self.assertEqual(recovered[0].transport_batch, 0)
                self.assertEqual(recovered[0].recovery_hash, report.semantic_hash)
                self.assertEqual(
                    before, (snapshot.semantic_hash, asdict(run), report.to_json())
                )
                unsafe = replace(
                    report, rows=(replace(report.rows[0], retry_safe=False),)
                )
                with self.assertRaisesRegex(WorkspaceError, "not safe to resume"):
                    classify_recovery(snapshot, run, unsafe)

    def test_update_retry_requires_exact_receipt_and_reviewed_fields(self):
        snapshot, run, report = _fixture()
        row = replace(snapshot.rows[0], disposition="UPDATE")
        snapshot = replace(snapshot, rows=(row,), counts={"UPDATE": 1})
        attempt = replace(
            run.rows[0], operation="UPDATE", transport_phase="UPDATE", odoo_id=7
        )
        run = replace(run, rows=(attempt,), snapshot_hash=snapshot.semantic_hash)
        outcome = replace(
            report.rows[0],
            operation="UPDATE",
            status=ReconciliationRowStatus.DIFFERENT,
            differing_fields=("name",),
            odoo_id=7,
        )
        report = replace(report, rows=(outcome,), snapshot_hash=run.snapshot_hash)
        recovered = classify_recovery(snapshot, run, report)
        self.assertEqual(recovered[0].status, ExecutionRowStatus.RETRY_READY)
        self.assertEqual(recovered[0].odoo_id, 7)
        for changed in (
            replace(outcome, odoo_id=8),
            replace(outcome, differing_fields=("other",)),
        ):
            with (
                self.subTest(outcome=changed),
                self.assertRaisesRegex(WorkspaceError, "not safe to resume"),
            ):
                classify_recovery(snapshot, run, replace(report, rows=(changed,)))

    def test_verified_create_keeps_pending_generated_receipt_partial(self):
        snapshot, run, report = _fixture()
        consumer = replace(
            snapshot.rows[0],
            row_id="consumer",
            dataset="consumers",
            fields=(
                FieldIntent(
                    "generated_id",
                    "SET_VALUE",
                    kind="relation",
                    related_model="x.generated",
                    incoming_projection_field="generated_id",
                    dependency_row_ids=("parent",),
                ),
            ),
        )
        snapshot = replace(snapshot, rows=(*snapshot.rows, consumer))
        consumer_attempt = replace(
            run.rows[0],
            row_id="consumer",
            dataset="consumers",
            status=ExecutionRowStatus.PLANNED,
        )
        run = replace(
            run,
            rows=(*run.rows, consumer_attempt),
            snapshot_hash=snapshot.semantic_hash,
        )
        report = replace(
            report,
            snapshot_hash=run.snapshot_hash,
            rows=(
                replace(
                    report.rows[0], status=ReconciliationRowStatus.VERIFIED, odoo_id=7
                ),
                replace(
                    report.rows[0],
                    row_id="consumer",
                    dataset="consumers",
                    execution_status="PLANNED",
                    status=ReconciliationRowStatus.NOT_WRITTEN,
                ),
            ),
        )
        self.assertEqual(
            projected_receipt_requirements(snapshot),
            {"parent": frozenset({("generated_id", "x.generated")})},
        )
        recovered = classify_recovery(snapshot, run, report)
        self.assertEqual(recovered[0].status, ExecutionRowStatus.PARTIALLY_APPLIED)
        self.assertEqual(recovered[0].odoo_id, 7)
        receipt = ProjectedOdooReceipt("generated_id", "x.generated", 70)
        run = replace(
            run,
            rows=(
                replace(run.rows[0], projected_receipts=(receipt,)),
                consumer_attempt,
            ),
        )
        self.assertEqual(
            classify_recovery(snapshot, run, report)[0].status,
            ExecutionRowStatus.COMMITTED,
        )

    def test_partial_relationship_never_becomes_a_second_create(self):
        snapshot, run, report = _fixture()
        snapshot = replace(
            snapshot,
            rows=(
                replace(
                    snapshot.rows[0],
                    fields=(
                        FieldIntent("parent_id", "SET_VALUE", defer_on_create=True),
                    ),
                ),
            ),
        )
        run = replace(
            run,
            snapshot_hash=snapshot.semantic_hash,
            rows=(
                replace(
                    run.rows[0], status=ExecutionRowStatus.PARTIALLY_APPLIED, odoo_id=7
                ),
            ),
        )
        report = replace(
            report,
            snapshot_hash=run.snapshot_hash,
            rows=(
                replace(
                    report.rows[0],
                    execution_status="PARTIALLY_APPLIED",
                    status=ReconciliationRowStatus.DIFFERENT,
                    odoo_id=7,
                    differing_fields=("parent_id",),
                ),
            ),
        )
        recovered = classify_recovery(snapshot, run, report)
        self.assertEqual(recovered[0].status, ExecutionRowStatus.PARTIALLY_APPLIED)
        self.assertEqual(recovered[0].odoo_id, 7)
        self.assertEqual(recovered[0].attempt, 1)
        report = replace(
            report, rows=(replace(report.rows[0], differing_fields=("name",)),)
        )
        with self.assertRaisesRegex(
            WorkspaceError, "partial relationship result is not safe"
        ):
            classify_recovery(snapshot, run, report)

    def test_paused_rejections_remain_ineligible_for_retry(self):
        snapshot, run, report = _fixture()
        for status in (ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED):
            with self.subTest(status=status):
                run = replace(
                    run,
                    load_control=LoadControl(
                        policy=RejectionPolicy.CONTINUE, state=LoadControlState.PAUSED
                    ),
                    rows=(replace(run.rows[0], status=status),),
                )
                report = replace(
                    report,
                    rows=(
                        replace(
                            report.rows[0],
                            execution_status=status.value,
                            status=ReconciliationRowStatus.NOT_WRITTEN,
                        ),
                    ),
                )
                self.assertEqual(
                    classify_recovery(snapshot, run, report)[0].status, status
                )
                active = replace(
                    run,
                    load_control=replace(
                        run.load_control, state=LoadControlState.RUNNING
                    ),
                )
                with self.assertRaisesRegex(
                    WorkspaceError, "stopped load cannot resume"
                ):
                    classify_recovery(snapshot, active, report)

    def test_changed_binding_rejects_evidence_with_existing_failure_code(self):
        snapshot, run, report = _fixture()
        for changed in (
            replace(report, target_database="other"),
            replace(report, execution_run_id="other"),
        ):
            with (
                self.subTest(report=changed),
                self.assertRaises(WorkspaceError) as raised,
            ):
                classify_recovery(snapshot, run, changed)
            self.assertEqual(
                raised.exception.load_failure_code,
                "LOAD_THE_RECOVERY_REPORT_BELONGS_TO_ANOTHER_LOAD",
            )
        run = replace(
            run,
            write_credential_binding_hash=HASH,
            write_principal_hash=HASH,
            write_permission_hash=HASH,
            write_context_hash=HASH,
        )
        report = replace(
            report,
            verification_credential_binding_hash=HASH,
            verification_principal_hash=HASH,
            verification_permission_hash=HASH,
            verification_context_hash="sha256:" + "2" * 64,
        )
        with self.assertRaisesRegex(
            WorkspaceError, "another Odoo principal or context"
        ):
            classify_recovery(snapshot, run, report)

    def test_targeted_read_retains_exact_committed_dependency_frontier(self):
        snapshot, run, report = _fixture()
        parent = replace(run.rows[0], status=ExecutionRowStatus.COMMITTED, odoo_id=7)
        consumer = replace(
            snapshot.rows[0],
            row_id="consumer",
            fields=(
                FieldIntent(
                    "parent_id",
                    "SET_VALUE",
                    dependency_row_ids=("parent",),
                ),
            ),
        )
        unrelated = replace(snapshot.rows[0], row_id="unrelated", fields=())
        snapshot = replace(snapshot, rows=(*snapshot.rows, consumer, unrelated))
        waiting = replace(run.rows[0], row_id="consumer")
        retained = replace(parent, row_id="unrelated", odoo_id=8)
        run = replace(
            run, snapshot_hash=snapshot.semantic_hash, rows=(parent, waiting, retained)
        )
        self.assertEqual(
            targeted_recovery_row_ids(snapshot, run), frozenset({"parent", "consumer"})
        )
        report = replace(
            report,
            snapshot_hash=run.snapshot_hash,
            readback_scope="RECOVERY_TARGETED",
            rows=(
                replace(
                    report.rows[0],
                    execution_status="COMMITTED",
                    status=ReconciliationRowStatus.VERIFIED,
                    odoo_id=7,
                ),
                replace(report.rows[0], row_id="consumer"),
            ),
        )
        recovered = classify_recovery(snapshot, run, report, targeted=True)
        self.assertEqual(
            recovered[2], replace(retained, recovery_hash=report.semantic_hash)
        )
        with self.assertRaisesRegex(WorkspaceError, "does not cover every load row"):
            classify_recovery(
                snapshot, run, replace(report, rows=report.rows[1:]), targeted=True
            )
        broken = replace(
            snapshot,
            rows=(
                snapshot.rows[0],
                replace(
                    consumer,
                    fields=(
                        FieldIntent(
                            "parent_id",
                            "SET_VALUE",
                            dependency_row_ids=("absent",),
                        ),
                    ),
                ),
                unrelated,
            ),
        )
        with self.assertRaisesRegex(WorkspaceError, "dependency is missing"):
            targeted_recovery_row_ids(broken, run)
