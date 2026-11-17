"""Qualify complete mixed-write jobs and durable worker recovery on real stores."""

from unittest.mock import patch

from impodo.domain.workspace.errors import WorkspaceError
from tests.support.hierarchy_assertions import assert_loaded_hierarchy
from tests.support.hierarchy_execution import json2_adapters
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.load_worker_performance import (
    measure_worker,
    queue_worker,
    worker_boundaries,
    worker_fixture,
)

MATRIX = ((19, "LOCAL"), (19, "REMOTE"), (20, "LOCAL"), (20, "REMOTE"))


class LoadWorkerPerformanceTests(HierarchyWorkflowFixture):
    def worker_fixture(self, major, mode):
        """Expose the exact same workload to isolated before/after counter runners."""

        return worker_fixture(self, major=major, mode=mode)

    def test_odoo19_local_complete_job(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote_complete_job(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local_complete_job(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote_complete_job(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        packet = self.worker_fixture(major, mode)
        queued = queue_worker(self, packet)
        self.assertEqual(queued.total_rows, 13)
        self.assertEqual(queued.target_database, packet.snapshot.target_database)
        measured = measure_worker(self, packet, queued=queued)
        self.assertTrue(measured.result.verification_complete)
        self.assertEqual(
            measured.result.completion_warning_code,
            "CORRECTION_ORIGIN_PREPARED_MISSING",
        )
        remote = mode == "REMOTE"
        self.assertLessEqual(measured.timings.connection_count, 172 if remote else 156)
        self.assertLessEqual(measured.timings.schema_check_count, 108 if remote else 98)
        self.assertEqual(measured.timings.lock_retry_count, 0)
        self.assertLessEqual(measured.sql_count, 847 if remote else 772)
        # Matched real-store jobs establish these per-mode ceilings.
        # Complete-job costs include verification and correction publication;
        # journal phases remain separately visible rather than absorbing them.
        for phase in (
            "journal_before_call",
            "journal_after_call",
            "progress_publication",
        ):
            self.assertGreater(measured.local_phases[phase]["calls"], 0)
            self.assertEqual(
                measured.local_phases[phase].get("capture_failure_count", 0), 0
            )
        stages = {
            facts["stage"]: facts["performance"]
            for event, facts in measured.events
            if event == "stage_finished"
        }
        for stage in ("target_authority", "write", "verification"):
            self.assertGreaterEqual(stages[stage]["sql_statement_count"], 0)
        for stage, limits in {
            "target_authority": (3, 2, 12) if remote else (1, 0, 3),
            "write": (52, 30, 401) if remote else (40, 24, 344),
            "verification": (60, 35, 222) if remote else (58, 33, 213),
        }.items():
            for metric, limit in zip(
                (
                    "database_connection_count",
                    "database_schema_check_count",
                    "sql_statement_count",
                ),
                limits,
                strict=True,
            ):
                self.assertLessEqual(stages[stage][metric], limit, (stage, metric))
        transport_calls = sum(
            operation in {"create", "load", "write"}
            for _model, operation in packet.target.wire_calls
        )
        for phase in ("journal_before_call", "journal_after_call"):
            costs = measured.local_phases[phase]
            self.assertEqual(costs["calls"], transport_calls)
            # Each receipt needs a linkage owner and an engine owner. The
            # first before-call receipt also performs the existing full check.
            self.assertLessEqual(
                costs["database_connection_count"], 2 * transport_calls
            )
            self.assertLessEqual(
                costs["database_schema_check_count"],
                transport_calls + (phase == "journal_before_call"),
            )
        for phase, limits in {
            "worker_admission": (3, 2),
            "execution_checks": (12, 9),
            "progress_publication": (0, 0),
        }.items():
            costs = measured.local_phases[phase]
            self.assertLessEqual(costs["database_connection_count"], limits[0], phase)
            self.assertLessEqual(costs["database_schema_check_count"], limits[1], phase)
        self.assertGreater(stages["verification"]["database_connection_count"], 0)
        run = self.context.execution.journal.get_run(
            packet.workspace_id, measured.result.execution_run_id
        )
        self.assertEqual(run.status.value, "COMPLETED")
        self.assertEqual(
            (run.committed_count, run.unknown_count, run.failed_count), (13, 0, 0)
        )
        self.assertEqual({row.operation for row in run.rows}, {"CREATE", "UPDATE"})
        self.assertEqual(sum(row.operation == "CREATE" for row in run.rows), 12)
        self.assertEqual(sum(row.operation == "UPDATE" for row in run.rows), 1)
        self.assertEqual(measured.progress[-1].run_id, run.run_id)
        self.assertEqual(measured.progress[-1].committed_count, 13)
        self.assertFalse(hasattr(measured.progress[-1], "rows"))
        self.assert_receipt_order(measured.ledger, run)
        self.assert_target_payloads(packet.target)
        report = self.context.reconciliation.current(packet.workspace_id)
        self.assertEqual(report.status.value, "VERIFIED")
        self.assertEqual(report.verified_count, 18)
        self.assertEqual(report.execution_run_id, run.run_id)
        writes = tuple(packet.target.writes)
        self.reopen()
        self.assertEqual(
            self.context.execution.journal.get_run(packet.workspace_id, run.run_id), run
        )
        self.assertEqual(
            self.context.reconciliation.current(packet.workspace_id).semantic_hash,
            report.semantic_hash,
        )
        self.assertEqual(tuple(packet.target.writes), writes)

    def assert_target_payloads(self, target):
        assert_loaded_hierarchy(self, target)
        self.assertEqual(target.updates, [("product.template", 501, {"name": "PX101"})])
        self.assertEqual(
            target.records[("product.template", 501)],
            {"default_code": "PX101", "name": "PX101", "categ_id": 104},
        )
        self.assertEqual(
            {
                values["name"]
                for (model, _), values in target.records.items()
                if model == "product.template"
            },
            {"PX101", "PX102", "PX103", "PX104", "PX105", "PX106"},
        )
        self.assertEqual(
            sum(
                len(ids)
                for model, _, ids in target.writes
                if model == "product.category"
            ),
            7,
        )
        self.assertEqual(
            sum(
                len(ids)
                for model, _, ids in target.writes
                if model == "product.template"
            ),
            6,
        )

    def assert_receipt_order(self, ledger, run):
        pending, dispatched, committed = None, False, {}
        for label, identifier, rows in ledger:
            if label == "before":
                self.assertIsNone(
                    pending, "A new batch preceded the previous durable outcome"
                )
                self.assertEqual(identifier, run.run_id)
                self.assertTrue(all(row.status.value == "IN_FLIGHT" for row in rows))
                self.assertEqual(
                    len(
                        {
                            (
                                row.transport_phase,
                                row.schedule_component,
                                row.transport_page,
                                row.transport_batch,
                                row.attempt,
                            )
                            for row in rows
                        }
                    ),
                    1,
                )
                pending, dispatched = rows, False
            elif label == "remote" and identifier in {"create", "load", "write"}:
                self.assertIsNotNone(
                    pending, "Odoo dispatch preceded its durable in-flight receipt"
                )
                self.assertFalse(dispatched)
                self.assertEqual({row.target_model for row in pending}, {rows})
                self.assertEqual(
                    {row.operation for row in pending},
                    {"UPDATE" if identifier == "write" else "CREATE"},
                )
                dispatched = True
            elif label == "after":
                self.assertTrue(dispatched)
                self.assertEqual(
                    {(row.row_id, row.attempt) for row in rows},
                    {(row.row_id, row.attempt) for row in pending},
                )
                self.assertTrue(all(row.status.value == "COMMITTED" for row in rows))
                committed.update({row.row_id: row for row in rows})
                pending = None
        self.assertIsNone(pending)
        self.assertEqual(committed, {row.row_id: row for row in run.rows})

    def test_interrupted_after_target_commit_reopens_and_recovers_without_duplicate_writes(
        self,
    ):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                packet = self.worker_fixture(major, mode)
                writer, _ = json2_adapters(self, packet.workspace_id, packet.target)

                def interrupt():
                    raise RuntimeError(
                        "Fictional process interruption after target commit"
                    )

                packet.target.after_create = interrupt
                with (
                    worker_boundaries(self, packet) as boundary,
                    self.assertRaisesRegex(
                        RuntimeError, "Fictional process interruption"
                    ),
                ):
                    self.context.execution.execute(
                        packet.workspace_id,
                        expected_snapshot_hash=packet.snapshot.semantic_hash,
                        executor=writer,
                        actor=self.actor,
                        **packet.identities,
                    )
                packet.target.after_create = None
                interrupted = self.context.execution.journal.get_current_run(
                    packet.workspace_id
                )
                self.assertEqual(interrupted.status.value, "RUNNING")
                self.assertGreater(interrupted.in_flight_count, 0)
                self.assertEqual(interrupted.committed_count, 0)
                self.assertEqual(
                    sum(event[0] == "before" for event in boundary.ledger), 1
                )
                self.assertFalse(any(event[0] == "after" for event in boundary.ledger))
                self.assertEqual(len(packet.target.writes), 1)
                existing = {
                    key: dict(value)
                    for key, value in packet.target.records.items()
                    if key != ("product.template", 501)
                }
                self.reopen()
                self.assertEqual(
                    self.context.execution.journal.get_current_run(packet.workspace_id),
                    interrupted,
                )
                writer, reader = json2_adapters(
                    self, packet.workspace_id, packet.target
                )
                write = {
                    name: packet.identities[name]
                    for name in ("write_identity", "write_credential_binding_hash")
                }
                writes = tuple(packet.target.writes)
                with worker_boundaries(self, packet):
                    recovery = self.context.reconciliation.assess_recovery(
                        packet.workspace_id,
                        expected_execution_run_id=interrupted.run_id,
                        reader=reader,
                        actor=self.actor,
                        **write,
                    )
                self.assertEqual(tuple(packet.target.writes), writes)
                plan = self.context.execution.review_recovery(
                    packet.workspace_id,
                    expected_execution_run_id=interrupted.run_id,
                    recovery=recovery,
                    actor=self.actor,
                    **write,
                )
                self.assertEqual(plan.safe_to_retry_count, 0)
                self.assertGreater(plan.resume_count, 0)
                with worker_boundaries(self, packet):
                    completed = self.context.execution.resume(
                        packet.workspace_id,
                        expected_execution_run_id=interrupted.run_id,
                        recovery=recovery,
                        executor=writer,
                        actor=self.actor,
                        **write,
                    )
                    verified = self.context.reconciliation.reconcile(
                        packet.workspace_id,
                        expected_execution_run_id=completed.run_id,
                        reader=reader,
                        actor=self.actor,
                        **write,
                    )
                self.assertEqual(completed.status.value, "COMPLETED")
                self.assertEqual(completed.committed_count, 13)
                self.assertTrue(
                    all(
                        packet.target.records[key] == value
                        for key, value in existing.items()
                    )
                )
                self.assertEqual(verified.status.value, "VERIFIED")
                self.assertEqual(verified.verified_count, 18)
                self.assert_target_payloads(packet.target)
                self.reopen()
                self.assertEqual(
                    self.context.execution.journal.get_run(
                        packet.workspace_id, completed.run_id
                    ),
                    completed,
                )

    def test_unknown_committed_outcome_reopens_and_readback_never_retries_it(self):
        for major, mode in MATRIX:
            with self.subTest(major=major, mode=mode):
                packet = self.worker_fixture(major, mode)
                writer, _ = json2_adapters(self, packet.workspace_id, packet.target)
                transport = writer.transport
                attempted = []

                def lose_response(url, *args, transport=transport, attempted=attempted):
                    result = transport(url, *args)
                    if url.endswith(("/create", "/load")):
                        attempted.append(url)
                        raise TimeoutError("Fictional response lost after commit")
                    return result

                with (
                    worker_boundaries(self, packet) as boundary,
                    patch.object(writer, "transport", side_effect=lose_response),
                ):
                    unknown = self.context.execution.execute(
                        packet.workspace_id,
                        expected_snapshot_hash=packet.snapshot.semantic_hash,
                        executor=writer,
                        actor=self.actor,
                        **packet.identities,
                    )
                self.assertEqual(unknown.status.value, "OUTCOME_UNKNOWN")
                self.assertGreater(unknown.unknown_count, 0)
                self.assertEqual(unknown.committed_count, 0)
                self.assertEqual(len(attempted), 1)
                self.assertEqual(len(packet.target.writes), 1)
                self.assertTrue(
                    any(
                        event[0] == "after"
                        and any(
                            row.status.value == "OUTCOME_UNKNOWN" for row in event[2]
                        )
                        for event in boundary.ledger
                    )
                )
                self.reopen()
                self.assertEqual(
                    self.context.execution.journal.get_run(
                        packet.workspace_id, unknown.run_id
                    ),
                    unknown,
                )
                writer, reader = json2_adapters(
                    self, packet.workspace_id, packet.target
                )
                before = tuple(packet.target.writes)
                with worker_boundaries(self, packet):
                    report = self.context.reconciliation.reconcile(
                        packet.workspace_id,
                        expected_execution_run_id=unknown.run_id,
                        reader=reader,
                        actor=self.actor,
                        write_identity=packet.identities["write_identity"],
                        write_credential_binding_hash=packet.identities[
                            "write_credential_binding_hash"
                        ],
                    )
                    with self.assertRaises(WorkspaceError):
                        self.context.execution.execute(
                            packet.workspace_id,
                            expected_snapshot_hash=packet.snapshot.semantic_hash,
                            executor=writer,
                            actor=self.actor,
                            **packet.identities,
                        )
                self.assertEqual(report.execution_run_id, unknown.run_id)
                self.assertGreater(report.verified_count, 5)
                self.assertEqual(tuple(packet.target.writes), before)
                self.assertEqual(
                    self.context.execution.journal.get_run(
                        packet.workspace_id, unknown.run_id
                    ),
                    unknown,
                )
