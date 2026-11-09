"""Exercise a committed BoM whose JSON-2 reply never reached Impodo."""

from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.reconciliation import ReconciliationRowStatus
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts.practice_customer_execution import (
    classifications,
    controlled_ports,
)
from scripts.qualify_practice_manufacturing import check_snapshot


def recover_south_bom(fixture, workspace, target):
    """Verify the stopped journal and load only the rows left by a fresh check."""
    fixture.prepare(workspace)
    fixture.approve(workspace)
    fixture.expect_business_writes(workspace)
    target.schema = fixture.context.queries.get_odoo_schema_catalog(workspace)
    target.target_hash = target.schema.connection_target_hash

    def compare(expected):
        report = fixture.context.preflight.compare(
            workspace,
            reader=target.compare,
            actor=fixture.actor,
            local_operation=run_local_operation_sync,
        )
        fixture.assertEqual(report.status, "READY", report)
        preview = fixture.context.execution.current_preview(
            workspace, actor=fixture.actor
        )
        fixture.assertEqual(dict(preview.snapshot.counts), expected)
        return preview.snapshot

    snapshot = compare(classifications(21, 0, 0))
    check_snapshot(fixture, snapshot, phase="boms", company="SOUTH")

    writer, reader, identities = controlled_ports(fixture, workspace, target, snapshot)
    first_write = len(target.writes)
    operation = "load" if target.schema.connection_mode == "REMOTE" else "create"
    target.lose_next_response = ("mrp.bom", operation)
    stopped = fixture.context.execution.execute(
        workspace,
        expected_snapshot_hash=snapshot.semantic_hash,
        executor=writer,
        actor=fixture.actor,
        batch_rows=1,
        **identities,
    )
    fixture.assertEqual(stopped.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
    fixture.assertEqual((stopped.unknown_count, stopped.committed_count), (1, 0))
    [(model, _values, (identifier,))] = target.writes[first_write:]
    fixture.assertEqual(model, "mrp.bom")
    code = target.records[model, identifier]["code"]
    (unknown,) = (
        row for row in stopped.rows if row.status is ExecutionRowStatus.OUTCOME_UNKNOWN
    )
    fixture.assertEqual(unknown.target_model, "mrp.bom")

    fixture.reopen()
    journal = fixture.context.execution.journal
    fixture.assertEqual(journal.get_current_run(workspace), stopped)
    evidence = fixture.context.reconciliation.reconcile(
        workspace,
        expected_execution_run_id=stopped.run_id,
        reader=reader,
        actor=fixture.actor,
        write_identity=identities["write_identity"],
        write_credential_binding_hash=identities["write_credential_binding_hash"],
    )
    fixture.assertEqual(evidence.verified_count, 1, evidence.rows)
    readback = next(row for row in evidence.rows if row.row_id == unknown.row_id)
    fixture.assertEqual(
        (readback.status, readback.odoo_id),
        (ReconciliationRowStatus.VERIFIED, identifier),
    )
    fixture.assertEqual(len(target.writes), first_write + 1)

    fresh = compare(classifications(20, 0, 1))
    (unchanged,) = (row for row in fresh.rows if row.disposition == "UNCHANGED")
    fixture.assertEqual(
        (unchanged.target_model, unchanged.business_identity), ("mrp.bom", (code,))
    )
    fixture.assertNotEqual(fresh.semantic_hash, snapshot.semantic_hash)
    writer, reader, identities = controlled_ports(fixture, workspace, target, fresh)
    completed = fixture.context.execution.execute(
        workspace,
        expected_snapshot_hash=fresh.semantic_hash,
        executor=writer,
        actor=fixture.actor,
        **identities,
    )
    fixture.assertEqual(completed.status, ExecutionRunStatus.COMPLETED, completed.rows)
    fixture.assertEqual(completed.committed_count, 20)
    verified = fixture.context.reconciliation.reconcile(
        workspace,
        expected_execution_run_id=completed.run_id,
        reader=reader,
        actor=fixture.actor,
        write_identity=identities["write_identity"],
        write_credential_binding_hash=identities["write_credential_binding_hash"],
    )
    fixture.assertEqual(verified.status.value, "VERIFIED", verified.rows)
    fixture.assertEqual(verified.verified_count, 21)
    compare(classifications(0, 0, 21))
    fixture.assertEqual(
        fixture.context.execution.journal.get_run(workspace, stopped.run_id), stopped
    )
    fixture.assertEqual(
        [
            row_id
            for (model, row_id), row in target.records.items()
            if model == "mrp.bom" and row["code"] == code
        ],
        [identifier],
    )
    fixture.assertEqual(sum(len(ids) for _, _, ids in target.writes[first_write:]), 21)
