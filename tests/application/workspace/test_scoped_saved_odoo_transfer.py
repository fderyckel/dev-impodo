"""Qualify saved restore policies with existing-parent scope in controlled fixtures.

The real matching, review, preflight, compiler and independent reconciliation
run here; fictional connector records do not establish live Odoo qualification.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from impodo.application.destination_matching_service import DestinationMatchingService
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.application.workspace.execution.reconciliation import ReconciliationService
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_readback import ExternalIdBinding
from impodo.domain.odoo_restore import (
    RestoreFieldRule,
    RestoreValuePolicy,
    compile_odoo_restore,
)
from impodo.domain.reconciliation import ReconciliationRunStatus
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.shared.models import BusinessReference
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from tests.application.workspace import test_scoped_odoo_transfer as scope_fixture
from tests.application.workspace.execution import (
    test_reconciliation as readback_fixture,
)


def _saved_fixture(major, mode):
    fixture = scope_fixture.ScopedTransferFixture(major, mode)
    fixture.workspace = replace(
        fixture.workspace, saved_restore_generation_id=str(uuid4())
    )
    fixture.schema = replace(
        fixture.schema,
        models=tuple(
            replace(
                model,
                fields=tuple(
                    replace(
                        field,
                        stored=True,
                        computed=False,
                        related=False,
                        company_dependent=False,
                        exportable=True,
                    )
                    for field in model.fields
                ),
            )
            for model in fixture.schema.models
        ),
    )
    fixture.metadata = replace(
        fixture.metadata,
        models={
            name: replace(
                model,
                fields={
                    field: replace(
                        value,
                        stored=True,
                        computed=False,
                        related=False,
                        company_dependent=False,
                        exportable=True,
                    )
                    for field, value in model.fields.items()
                },
            )
            for name, model in fixture.metadata.models.items()
        },
    )
    fixture.targets["x.scope_record"] = (
        replace(
            fixture.targets["x.scope_record"][0],
            values={
                "default_code": "SAME",
                "name": "Destination first",
                "parent_id": 901,
            },
        ),
    )
    fixture.choices = tuple(
        replace(choice, destination_handling="reuse_only")
        if choice.dataset_id == fixture.parent.dataset_id
        else choice
        for choice in fixture.choices
    )
    return fixture


def _plans(fixture, rules=()):
    child = next(
        model for model in fixture.schema.models if model.name == "x.scope_record"
    )
    return {
        child.name: compile_odoo_restore(
            child,
            child,
            source_version=fixture.schema.odoo_version,
            destination_version=fixture.schema.odoo_version,
            captured_fields=("default_code", "name", "parent_id"),
            identity_fields=("default_code", "parent_id"),
            rules=rules,
        )
    }


def _match(fixture, plans):
    fixture.captured.clear()
    return DestinationMatchingService(fixture).check(
        fixture.workspace,
        fixture.selection,
        fixture.schema,
        fixture.choices,
        api_key="fixture-key",
        credential_binding_hash=scope_fixture.BINDING_HASH,
        read_identity=fixture.identity,
        reader=fixture.reader,
        recorded_by="Data manager",
        source_origins=fixture.origins,
        record_snapshot_sink=fixture.captured.append,
        restore_plans=plans,
    )


def _compile(fixture, plans):
    match = _match(fixture, plans)
    matched = replace(fixture.workspace, destination_match_plan=match)
    order = TransferOrderService().build(matched, match, recorded_by="Data manager")
    ordered = replace(matched, transfer_order_plan=order)
    package = TransferReviewService().build(
        ordered,
        match,
        order,
        run_id=str(uuid4()),
        data_version_id=fixture.selection.data_version_id,
        built_by=LOCAL_ACTOR.identity,
        model_policies={"x.scope_parent": "reuse_only"},
        restore_plans=plans,
    )
    approval = TransferReviewApproval.approve(
        package, approval_id=str(uuid4()), actor=LOCAL_ACTOR, approved_at=fixture.now
    )
    approved = replace(
        ordered, transfer_review_package=package, transfer_review_approval=approval
    )
    fresh = _match(fixture, plans)
    report = TransferPreflightService().build(
        approved,
        package,
        approval,
        match,
        fresh,
        recorded_by=LOCAL_ACTOR.identity,
        restore_plans=plans,
    )
    workspace = replace(approved, transfer_preflight_report=report)
    snapshot = compile_transfer_execution_snapshot(
        workspace,
        fixture.selection,
        fixture.schema,
        package,
        report,
        fresh,
        fixture.captured[0],
        source_rows=fixture.rows,
        source_origins=fixture.origins,
        source_snapshots={
            item.dataset_id: SimpleNamespace(content_hash=scope_fixture.HASH)
            for item in fixture.selection.datasets
        },
        source_manifest_hashes={
            item.dataset_id: scope_fixture.HASH for item in fixture.selection.datasets
        },
        restore_plans=plans,
    )
    return SimpleNamespace(
        snapshot=snapshot, match=match, fresh=fresh, report=report, package=package
    )


def _reconcile(fixture, snapshot, now, *, wrong_parent=False):
    identifiers = {
        ("x.scope_parent", 1): 901,
        ("x.scope_parent", 2): 902,
        ("x.scope_record", 1): 1001,
        ("x.scope_record", 2): 1002,
    }
    attempts = tuple(
        ExecutionRowAttempt(
            row_id=row.row_id,
            dataset=row.dataset,
            source_row=row.source_row,
            target_model=row.target_model,
            operation=row.disposition,
            field_names=tuple(intent.field for intent in row.fields),
            proposed_external_id=row.proposed_external_id,
            status=ExecutionRowStatus.COMMITTED,
            attempt=1,
            odoo_id=identifiers[(row.target_model, row.source_row)],
        )
        for row in snapshot.rows
        if row.disposition in {"CREATE", "UPDATE"}
    )
    run = ExecutionRun(
        run_id=str(uuid4()),
        workspace_id=snapshot.workspace_id,
        snapshot_hash=snapshot.semantic_hash,
        snapshot_root_hash=snapshot.root_hash,
        preflight_run_id=snapshot.preflight_run_id,
        target_hash=snapshot.target_hash,
        target_database=snapshot.target_database,
        batch_rows=10,
        status=ExecutionRunStatus.COMPLETED,
        started_at=now,
        started_by="Data manager",
        completed_at=now,
        rows=attempts,
    )
    reader = readback_fixture._Reader(execution_api_scope(snapshot).semantic_hash)
    reader.target_hash = snapshot.target_hash
    reader.records = {
        ("x.scope_parent", 901): {"name": "Parent A"},
        ("x.scope_parent", 902): {"name": "Parent B"},
        ("x.scope_record", 1001): {
            "default_code": "SAME",
            "name": "First",
            "parent_id": 901,
        },
        ("x.scope_record", 1002): {
            "default_code": "SAME",
            "name": "Second",
            "parent_id": 901 if wrong_parent else 902,
        },
    }
    reader.references = {"x.scope_parent": {"Parent A": 901, "Parent B": 902}}
    reader.external_ids = {
        row.proposed_external_id: ExternalIdBinding(
            row.proposed_external_id,
            row.target_model,
            identifiers[(row.target_model, row.source_row)],
        )
        for row in snapshot.rows
        if row.proposed_external_id
    }
    service = ReconciliationService(
        SimpleNamespace(execution_snapshot=lambda *_args: snapshot),
        readback_fixture._Execution(run),
        readback_fixture._Results(),
        CapabilityAuthorizationPolicy(),
        database_admission=fixture.require_current_destination,
    )
    return service.reconcile(
        snapshot.workspace_id,
        expected_execution_run_id=run.run_id,
        reader=reader,
        actor=LOCAL_ACTOR,
    )


class ScopedSavedOdooTransferTests(unittest.TestCase):
    def test_saved_policies_and_scope_reach_checked_execution_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for policy in RestoreValuePolicy:
                    with self.subTest(major=major, mode=mode, policy=policy):
                        fixture = _saved_fixture(major, mode)
                        source_hash = fixture.selection.content_hash
                        schema_hash = fixture.schema.content_hash
                        origins = dict(fixture.origins)
                        result = _compile(
                            fixture,
                            _plans(fixture, (RestoreFieldRule("name", policy=policy),)),
                        )
                        self.assertTrue(result.report.ready)
                        self.assertIsNotNone(result.package.restore_policy_hash)
                        children = tuple(
                            row
                            for row in result.snapshot.rows
                            if row.target_model == "x.scope_record"
                        )
                        self.assertNotEqual(
                            children[0].source_identity, children[1].source_identity
                        )
                        self.assertEqual(
                            children[0].business_scope,
                            (
                                BusinessReference(
                                    "x.scope_parent",
                                    ("Parent A",),
                                    target_fields=("name",),
                                ),
                            ),
                        )
                        self.assertEqual(
                            children[1].business_scope[0].key, ("Parent B",)
                        )
                        existing_fields = {
                            intent.field: intent.value for intent in children[0].fields
                        }
                        if policy is RestoreValuePolicy.USE_SAVED:
                            self.assertEqual(existing_fields["name"], "First")
                        else:
                            self.assertNotIn("name", existing_fields)
                        self.assertEqual(
                            next(
                                intent.value
                                for intent in children[1].fields
                                if intent.field == "name"
                            ),
                            "Second",
                        )
                        parents = tuple(
                            row
                            for row in result.snapshot.rows
                            if row.target_model == "x.scope_parent"
                        )
                        self.assertTrue(
                            all(
                                row.disposition == "UNCHANGED" and not row.fields
                                for row in parents
                            )
                        )
                        self.assertEqual(fixture.selection.content_hash, source_hash)
                        self.assertEqual(fixture.schema.content_hash, schema_hash)
                        self.assertEqual(fixture.origins, origins)

    def test_matching_scope_cannot_be_renamed_excluded_or_preserved_by_field_policy(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for rule in (
                    RestoreFieldRule("parent_id", target_field="x_parent_id"),
                    RestoreFieldRule("parent_id", excluded=True),
                    RestoreFieldRule(
                        "parent_id", policy=RestoreValuePolicy.KEEP_DESTINATION
                    ),
                    RestoreFieldRule("parent_id", policy=RestoreValuePolicy.FILL_EMPTY),
                ):
                    with self.subTest(major=major, mode=mode, rule=rule):
                        plan = _plans(_saved_fixture(major, mode), (rule,))[
                            "x.scope_record"
                        ]
                        self.assertFalse(plan.ready)
                        self.assertTrue(
                            any(
                                issue.code.startswith("RESTORE_IDENTITY_")
                                for issue in plan.issues
                            )
                        )

    def test_saved_restore_cannot_create_missing_scope_parents(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = _saved_fixture(major, mode)
                    fixture.targets["x.scope_parent"] = fixture.targets[
                        "x.scope_parent"
                    ][:1]
                    with self.assertRaisesRegex(
                        WorkspaceError, "parent must already match exactly one"
                    ):
                        _match(fixture, _plans(fixture))
                    self.assertEqual(len(fixture.calls), 1)

    def test_independent_readback_detects_a_scoped_child_under_the_wrong_parent(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = _saved_fixture(major, mode)
                    result = _compile(fixture, _plans(fixture))
                    verified = _reconcile(fixture, result.snapshot, fixture.now)
                    self.assertEqual(verified.status, ReconciliationRunStatus.VERIFIED)
                    wrong = _reconcile(fixture, result.snapshot, fixture.now, wrong_parent=True)
                    self.assertGreater(wrong.fallout_count, 0)
                    self.assertNotEqual(wrong.status, ReconciliationRunStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
