"""Verify exact-ID, bounded, journaled correction execution."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from impodo.application.correction_execution import (
    CorrectionExecutionService,
    correction_api_scope,
)
from impodo.application.correction_orchestration import CorrectionBinding
from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlanError,
    CorrectionValueKind,
)
from impodo.domain.correction_execution import CorrectionExecutionSnapshot
from impodo.domain.correction_origin import ProtectedCorrectionArtifactReference
from impodo.domain.execution.models import ExecutionRunStatus
from impodo.domain.execution.odoo_readback import ReadbackRecord
from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from tests.domain.test_correction_plan import (
    CONFIRMATION_ID,
    HASHES,
    NOW,
    _field,
    _write_identity,
    make_plan,
)

COMPLETED_WORKSPACE_ID = "77777777-7777-4777-8777-777777777777"


def _reference(identifier, logical_hash, name):
    return ProtectedCorrectionArtifactReference(
        artifact_id=identifier,
        logical_hash=logical_hash,
        storage_key=f"project/{name}.ipe",
        artifact_hash=HASHES[8],
    )


class _Bindings:
    def __init__(self, plan, confirmation):
        self.invalidations = 0
        self.completions = 0
        self.binding = CorrectionBinding(
            correction_binding_id="88888888-8888-4888-8888-888888888888",
            project_id=plan.project_id,
            data_version_id="99999999-9999-4999-8999-999999999999",
            completed_migration_run_id=plan.completed_migration_run_id,
            completed_workspace_id=COMPLETED_WORKSPACE_ID,
            origin=_reference(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", HASHES[4], "origin"
            ),
            target_index=_reference(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", HASHES[5], "index"
            ),
            successor_migration_run_id=plan.successor_migration_run_id,
            successor_workspace_id=plan.workspace_id,
            current_mapping_hash=HASHES[5],
            current_prepared_hash=plan.corrected_prepared_hash,
            current_plan=_reference(plan.plan_id, plan.plan_hash, "plan"),
            current_confirmation=_reference(
                confirmation.confirmation_id,
                confirmation.confirmation_hash,
                "confirmation",
            ),
            optimistic_revision=4,
            created_at=NOW,
            updated_at=NOW,
        )

    def get_for_completed_workspace(self, completed_workspace_id):
        return self.binding

    def invalidate_plan(self, *args, **kwargs):
        self.invalidations += 1
        self.binding = replace(
            self.binding,
            current_plan=None,
            current_confirmation=None,
            optimistic_revision=5,
        )
        return self.binding

    def complete_verified_successor(self, *args, **kwargs):
        self.completions += 1
        self.binding = replace(self.binding, optimistic_revision=5)
        return self.binding


class _Journal:
    def __init__(self):
        self.run = None
        self.events = []

    def start_run(self, workspace_id, run, *, actor, correction_plan_hash=""):
        self.run = run
        self.events.append(("start", correction_plan_hash))

    def record_batch_started(self, workspace_id, run_id, rows):
        self.events.append(("before-write", tuple(item.odoo_id for item in rows)))
        self._replace(rows)

    def record_outcomes(self, workspace_id, run_id, rows):
        self.events.append(("outcome", tuple(item.status.value for item in rows)))
        self._replace(rows)

    def _replace(self, changed):
        by_id = {item.row_id: item for item in changed}
        self.run = replace(
            self.run,
            rows=tuple(by_id.get(item.row_id, item) for item in self.run.rows),
        )

    def finish_run(self, workspace_id, run_id, status, *, actor):
        self.run = replace(
            self.run,
            status=status,
            completed_at=datetime.now(UTC),
        )
        self.events.append(("finish", status.value))
        return self.run


class _Results:
    def __init__(self):
        self.report = None

    def publish(self, workspace_id, report, *, actor):
        self.report = report


class _Target:
    def __init__(self, snapshot, *, stale=False, failure=""):
        self.target_hash = snapshot.target_hash
        self.scope_hash = correction_api_scope(snapshot).semantic_hash
        self.imports_external_ids = False
        self.values = {
            (record.target_model, record.odoo_id): {
                field.target_field: field.confirmed_current
                for field in record.fields
            }
            for record in snapshot.records
        }
        if stale:
            key = next(iter(self.values))
            field = next(iter(self.values[key]))
            self.values[key][field] = "concurrent-change"
        self.read_calls = []
        self.write_calls = []
        self.failure = failure

    def read_ids(self, model, identifiers, fields):
        self.read_calls.append((model, tuple(identifiers), tuple(fields)))
        return tuple(
            ReadbackRecord(identifier, dict(self.values[(model, identifier)]))
            for identifier in identifiers
        )

    def update_rows(self, model, record_ids, values):
        self.write_calls.append((model, tuple(record_ids), dict(values)))
        if self.failure == "rejected":
            raise OdooWriteRejected("rejected")
        if self.failure == "unknown":
            raise OdooWriteOutcomeUnknown("lost response")
        for identifier in record_ids:
            self.values[(model, identifier)].update(values)


class CorrectionExecutionServiceTests(unittest.TestCase):
    def _fixture(self, count, *, stale=False, failure=""):
        plan = make_plan(
            tuple(_field(index, "active", False, True) for index in range(1, count + 1))
        )
        confirmation = CorrectionConfirmation.create(
            confirmation_id=CONFIRMATION_ID,
            plan=plan,
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(),
            confirmed_by=plan.created_by,
            confirmed_at=NOW,
        )
        snapshot = CorrectionExecutionSnapshot.create(
            plan, confirmation, target_database="impodo-test"
        )
        target = _Target(snapshot, stale=stale, failure=failure)
        bindings = _Bindings(plan, confirmation)
        journal = _Journal()
        results = _Results()
        service = CorrectionExecutionService(
            bindings,
            object(),
            journal,
            results,
            CapabilityAuthorizationPolicy(),
        )
        return plan, confirmation, target, bindings, journal, results, service

    def test_768_equal_updates_use_sixteen_exact_id_writes(self):
        plan, confirmation, target, bindings, journal, _results, service = self._fixture(768)

        outcome = service.execute(
            COMPLETED_WORKSPACE_ID,
            plan,
            confirmation,
            target_database="impodo-test",
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(observed_at="2026-08-28T04:02:00Z"),
            reader=target,
            writer=target,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(target.write_calls), 16)
        self.assertEqual(len(target.read_calls), 32)
        self.assertTrue(all(len(item[1]) <= 50 for item in target.write_calls))
        self.assertEqual(outcome.execution.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(outcome.reconciliation.status.value, "VERIFIED")
        self.assertEqual(bindings.completions, 1)
        first_write = next(
            index for index, event in enumerate(journal.events) if event[0] == "before-write"
        )
        self.assertGreater(first_write, 0)

    def test_many2one_writes_only_exact_product_relationship_ids(self):
        field = replace(
            _field(1, "uom_id", 90, 91),
            value_kind=CorrectionValueKind.MANY2ONE,
            current=90,
        )
        plan = make_plan((field,))
        confirmation = CorrectionConfirmation.create(
            confirmation_id=CONFIRMATION_ID,
            plan=plan,
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(),
            confirmed_by=plan.created_by,
            confirmed_at=NOW,
        )
        snapshot = CorrectionExecutionSnapshot.create(
            plan,
            confirmation,
            target_database="impodo-test",
        )
        target = _Target(snapshot)
        bindings = _Bindings(plan, confirmation)
        service = CorrectionExecutionService(
            bindings,
            object(),
            _Journal(),
            _Results(),
            CapabilityAuthorizationPolicy(),
        )

        outcome = service.execute(
            COMPLETED_WORKSPACE_ID,
            plan,
            confirmation,
            target_database="impodo-test",
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(observed_at="2026-08-28T04:02:00Z"),
            reader=target,
            writer=target,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            target.write_calls,
            [("product.template", (701,), {"uom_id": 91})],
        )
        self.assertEqual(outcome.reconciliation.status.value, "VERIFIED")
        self.assertEqual(bindings.completions, 1)

    def test_changed_target_invalidates_review_before_journal_or_write(self):
        plan, confirmation, target, bindings, journal, _results, service = self._fixture(
            2, stale=True
        )

        with self.assertRaisesRegex(CorrectionPlanError, "changed after confirmation"):
            service.execute(
                COMPLETED_WORKSPACE_ID,
                plan,
                confirmation,
                target_database="impodo-test",
                write_credential_binding_hash=HASHES[11],
                write_identity=_write_identity(observed_at="2026-08-28T04:02:00Z"),
                reader=target,
                writer=target,
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(bindings.invalidations, 1)
        self.assertEqual(journal.events, [])
        self.assertEqual(target.write_calls, [])

    def test_unknown_batch_stops_later_writes_and_is_read_back(self):
        plan, confirmation, target, bindings, _journal, _results, service = self._fixture(
            51, failure="unknown"
        )

        outcome = service.execute(
            COMPLETED_WORKSPACE_ID,
            plan,
            confirmation,
            target_database="impodo-test",
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(observed_at="2026-08-28T04:02:00Z"),
            reader=target,
            writer=target,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(target.write_calls), 1)
        self.assertEqual(outcome.execution.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.execution.unknown_count, 50)
        self.assertEqual(outcome.execution.blocked_count, 1)
        self.assertEqual(outcome.reconciliation.status.value, "FALLOUT")
        self.assertEqual(outcome.reconciliation.retry_safe_count, 50)
        self.assertEqual(bindings.completions, 0)

    def test_known_rejection_remains_distinct_from_unknown(self):
        plan, confirmation, target, _bindings, _journal, _results, service = self._fixture(
            2, failure="rejected"
        )

        outcome = service.execute(
            COMPLETED_WORKSPACE_ID,
            plan,
            confirmation,
            target_database="impodo-test",
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(observed_at="2026-08-28T04:02:00Z"),
            reader=target,
            writer=target,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(outcome.execution.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(outcome.execution.failed_count, 2)
        self.assertEqual(outcome.execution.unknown_count, 0)


if __name__ == "__main__":
    unittest.main()
