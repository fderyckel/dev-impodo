"""Verify protected reference aliases through saved scope and independent read-back.

These fictional targets exercise the shared application contracts. They do not
qualify live Odoo writes or package import into another installation.
"""

import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from impodo.adapters.protected_destination_identity_aliases import (
    ProtectedDestinationIdentityAliasStore,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
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
from impodo.domain.execution.odoo_readback import ExternalIdBinding, ReadbackRecord
from impodo.domain.mapping.contracts import ValueMapping
from impodo.domain.odoo_restore import RestoreFieldRule, RestoreValuePolicy
from impodo.domain.reconciliation import ReconciliationRunStatus
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.shared.models import (
    BusinessReference,
    TargetRecord,
    target_record_binding_hash,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from tests.application.workspace import test_saved_odoo_transfer as native
from tests.application.workspace import test_scoped_saved_odoo_transfer as saved
from tests.application.workspace.test_destination_matching import HASH

ALIASES = (
    ValueMapping("Parent A", "Renamed parent A"),
    ValueMapping("Parent B", "Renamed parent B"),
)


def _fixture(major, mode, *, target_offset=0):
    fixture = saved._saved_fixture(major, mode)
    fixture.choices = (
        fixture.choices[0],
        replace(
            fixture.choices[1],
            destination_handling="reuse_only",
            identity_aliases=ALIASES,
        ),
    )
    fixture.targets["x.scope_parent"] = tuple(
        replace(
            record,
            odoo_id=record.odoo_id + target_offset,
            values={"name": alias.target_value},
        )
        for record, alias in zip(
            fixture.targets["x.scope_parent"], ALIASES, strict=True
        )
    )
    fixture.targets["x.scope_record"] = tuple(
        replace(
            record,
            odoo_id=record.odoo_id + target_offset,
            values={
                **record.values,
                "parent_id": record.values["parent_id"] + target_offset,
            },
        )
        for record in fixture.targets["x.scope_record"]
    )
    return fixture


def _compile(fixture, rules=()):
    plans = saved._plans(fixture, rules)
    match = saved._match(fixture, plans)
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
        package,
        approval_id=str(uuid4()),
        actor=LOCAL_ACTOR,
        approved_at=fixture.now,
    )
    approved = replace(
        ordered, transfer_review_package=package, transfer_review_approval=approval
    )
    fresh = saved._match(fixture, plans)
    report = TransferPreflightService().build(
        approved,
        package,
        approval,
        match,
        fresh,
        recorded_by=LOCAL_ACTOR.identity,
        restore_plans=plans,
    )
    snapshot = compile_transfer_execution_snapshot(
        replace(approved, transfer_preflight_report=report),
        fixture.selection,
        fixture.schema,
        package,
        report,
        fresh,
        fixture.captured[0],
        source_rows=fixture.rows,
        source_origins=fixture.origins,
        source_snapshots={
            item.dataset_id: SimpleNamespace(content_hash=HASH)
            for item in fixture.selection.datasets
        },
        source_manifest_hashes={
            item.dataset_id: HASH for item in fixture.selection.datasets
        },
        restore_plans=plans,
    )
    return SimpleNamespace(
        snapshot=snapshot,
        match=match,
        fresh=fresh,
        report=report,
        package=package,
        approval=approval,
        approved=approved,
        plans=plans,
    )


class _Readback:
    imports_external_ids = True

    def __init__(self, fixture, snapshot, *, wrong_parent=False, missing_create=False):
        self.target_hash = snapshot.target_hash
        self.scope_hash = execution_api_scope(snapshot).semantic_hash
        self.lookups = []
        parents = fixture.targets["x.scope_parent"]
        first_id = fixture.targets["x.scope_record"][0].odoo_id
        self.records = {
            "x.scope_parent": parents,
            "x.scope_record": (
                TargetRecord(
                    "x.scope_record",
                    first_id,
                    {
                        "default_code": "SAME",
                        "name": "First",
                        "parent_id": parents[0].odoo_id,
                    },
                ),
                *(
                    ()
                    if missing_create
                    else (
                        TargetRecord(
                            "x.scope_record",
                            first_id + 1,
                            {
                                "default_code": "SAME",
                                "name": "Second",
                                "parent_id": parents[0 if wrong_parent else 1].odoo_id,
                            },
                        ),
                    )
                ),
            ),
        }
        self.external_ids = {
            row.proposed_external_id: ExternalIdBinding(
                row.proposed_external_id, row.target_model, first_id + 1
            )
            for row in snapshot.rows
            if row.disposition == "CREATE"
            and row.proposed_external_id
            and not missing_create
        }

    def find_records_many(self, model, queries):
        self.lookups.extend((model, query.domain) for query in queries)
        return tuple(
            tuple(
                ReadbackRecord(record.odoo_id, record.values)
                for record in self.records.get(model, ())
                if all(
                    record.values.get(field) == value
                    for field, _operator, value in query.domain
                )
            )
            for query in queries
        )

    def read_ids(self, model, identifiers, fields):
        return tuple(
            ReadbackRecord(
                record.odoo_id, {field: record.values.get(field) for field in fields}
            )
            for record in self.records.get(model, ())
            if record.odoo_id in identifiers
        )

    def read_external_ids(self, identifiers):
        return tuple(
            self.external_ids[identifier]
            for identifier in identifiers
            if identifier in self.external_ids
        )


def _verification(fixture, snapshot, reader, *, recovery=False):
    first_id = fixture.targets["x.scope_record"][0].odoo_id
    run = ExecutionRun(
        run_id=str(uuid4()),
        workspace_id=snapshot.workspace_id,
        snapshot_hash=snapshot.semantic_hash,
        snapshot_root_hash=snapshot.root_hash,
        preflight_run_id=snapshot.preflight_run_id,
        target_hash=snapshot.target_hash,
        target_database=snapshot.target_database,
        batch_rows=10,
        status=ExecutionRunStatus.RUNNING if recovery else ExecutionRunStatus.COMPLETED,
        started_at=fixture.now,
        started_by="Data manager",
        completed_at=None if recovery else fixture.now,
        rows=tuple(
            ExecutionRowAttempt(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                field_names=tuple(intent.field for intent in row.fields),
                proposed_external_id=row.proposed_external_id,
                status=ExecutionRowStatus.IN_FLIGHT
                if recovery and row.disposition == "CREATE"
                else ExecutionRowStatus.COMMITTED,
                attempt=1,
                odoo_id=None
                if recovery and row.disposition == "CREATE"
                else first_id + (row.disposition == "CREATE"),
            )
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        ),
    )
    service = ReconciliationService(
        SimpleNamespace(execution_snapshot=lambda *_args: snapshot),
        SimpleNamespace(get_run=lambda *_args: run, get_current_run=lambda *_args: run),
        saved.readback_fixture._Results(),
        CapabilityAuthorizationPolicy(),
        database_admission=fixture.require_current_destination,
    )
    operation = service.assess_recovery if recovery else service.reconcile
    return operation(
        snapshot.workspace_id,
        expected_execution_run_id=run.run_id,
        reader=reader,
        actor=LOCAL_ACTOR,
    )


class AliasedSavedOdooTransferTests(unittest.TestCase):
    def test_native_account_company_membership_uses_reviewed_existing_company_alias(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                scenario = native._coa_scenario(
                    major,
                    destination_company="Company B",
                    identity_aliases=(ValueMapping("Company A", "Company B"),),
                )
                source_rows = deepcopy(scenario.fixture.rows)
                source_origins = deepcopy(scenario.fixture.origins)
                self.assertTrue(scenario.match.ready)
                matched = replace(
                    scenario.workspace, destination_match_plan=scenario.match
                )
                order = TransferOrderService().build(
                    matched, scenario.match, recorded_by="Data manager"
                )
                ordered = replace(matched, transfer_order_plan=order)
                package = TransferReviewService().build(
                    ordered,
                    scenario.match,
                    order,
                    run_id=str(uuid4()),
                    data_version_id=scenario.fixture.selection.data_version_id,
                    built_by=LOCAL_ACTOR.identity,
                    restore_plans=scenario.plans,
                )
                approval = TransferReviewApproval.approve(
                    package,
                    approval_id=str(uuid4()),
                    actor=LOCAL_ACTOR,
                    approved_at=scenario.fixture.now,
                )
                approved = replace(
                    ordered,
                    transfer_review_package=package,
                    transfer_review_approval=approval,
                )
                report = TransferPreflightService().build(
                    approved,
                    package,
                    approval,
                    scenario.match,
                    scenario.match,
                    recorded_by=LOCAL_ACTOR.identity,
                    restore_plans=scenario.plans,
                )
                self.assertTrue(report.ready)
                scenario.workspace = replace(approved, transfer_preflight_report=report)
                scenario.package, scenario.report = package, report
                snapshot = native._compile(scenario)
                new_account = next(
                    row
                    for row in snapshot.rows
                    if row.target_model == "account.account"
                    and row.disposition == "CREATE"
                )
                company_link = next(
                    intent
                    for intent in new_account.fields
                    if intent.field == "company_ids"
                )
                self.assertEqual(
                    company_link.value,
                    (BusinessReference("res.company", ("Company B",)),),
                )
                self.assertEqual(company_link.related_identity_fields, ("name",))
                self.assertEqual(
                    company_link.target_binding_hashes,
                    (target_record_binding_hash("res.company", 22),),
                )
                self.assertNotEqual(
                    company_link.target_binding_hashes,
                    (target_record_binding_hash("res.company", 7),),
                )
                company = next(
                    row for row in snapshot.rows if row.target_model == "res.company"
                )
                self.assertEqual(company.source_identity, ("Company A",))
                self.assertEqual(company.business_identity, ("Company B",))
                self.assertEqual(company.disposition, "UNCHANGED")
                self.assertEqual(company.fields, ())
                self.assertEqual(scenario.fixture.rows, source_rows)
                self.assertEqual(scenario.fixture.origins, source_origins)
                company_dataset = next(
                    item
                    for item in scenario.fixture.selection.datasets
                    if item.source.model == "res.company"
                )
                self.assertEqual(
                    scenario.fixture.rows[company_dataset.dataset_id][0].values,
                    {"name": "Company A"},
                )

    def test_saved_scope_uses_mapped_parent_without_rewriting_source_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for policy in RestoreValuePolicy:
                    with self.subTest(major=major, mode=mode, policy=policy):
                        fixture = _fixture(major, mode)
                        before = (
                            fixture.selection,
                            fixture.schema,
                            deepcopy(fixture.rows),
                            deepcopy(fixture.origins),
                        )
                        result = _compile(
                            fixture, (RestoreFieldRule("name", policy=policy),)
                        )
                        self.assertTrue(result.report.ready)
                        parents = tuple(
                            row
                            for row in result.snapshot.rows
                            if row.target_model == "x.scope_parent"
                        )
                        children = tuple(
                            row
                            for row in result.snapshot.rows
                            if row.target_model == "x.scope_record"
                        )
                        self.assertEqual(
                            tuple(row.business_identity for row in parents),
                            (("Renamed parent A",), ("Renamed parent B",)),
                        )
                        self.assertEqual(
                            tuple(row.source_identity for row in parents),
                            (("Parent A",), ("Parent B",)),
                        )
                        self.assertTrue(
                            all(
                                row.disposition == "UNCHANGED" and not row.fields
                                for row in parents
                            )
                        )
                        self.assertEqual(
                            children[0].business_scope,
                            (
                                BusinessReference(
                                    "x.scope_parent",
                                    ("Renamed parent A",),
                                    target_fields=("name",),
                                ),
                            ),
                        )
                        self.assertEqual(
                            children[1].business_scope[0].key, ("Renamed parent B",)
                        )
                        self.assertIn("Parent A", children[0].source_identity[0])
                        self.assertNotIn(
                            "Renamed parent A", children[0].source_identity[0]
                        )
                        self.assertEqual(
                            (
                                fixture.selection,
                                fixture.schema,
                                fixture.rows,
                                fixture.origins,
                            ),
                            before,
                        )
                        self.assertNotIn("Renamed parent", result.match.to_json())
                        self.assertNotIn("Renamed parent", result.package.to_json())

    def test_independent_readback_and_interrupted_recovery_use_destination_alias_scope(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = _fixture(major, mode)
                    snapshot = _compile(fixture).snapshot
                    reader = _Readback(fixture, snapshot)
                    self.assertEqual(
                        _verification(fixture, snapshot, reader).status,
                        ReconciliationRunStatus.VERIFIED,
                    )
                    wrong = _verification(
                        fixture,
                        snapshot,
                        _Readback(fixture, snapshot, wrong_parent=True),
                    )
                    self.assertGreater(wrong.fallout_count, 0)
                    recovery_reader = _Readback(fixture, snapshot, missing_create=True)
                    assessment = _verification(
                        fixture, snapshot, recovery_reader, recovery=True
                    )
                    create = next(
                        row for row in snapshot.rows if row.disposition == "CREATE"
                    )
                    self.assertTrue(
                        next(
                            row
                            for row in assessment.rows
                            if row.row_id == create.row_id
                        ).retry_safe
                    )
                    self.assertIn(
                        ("x.scope_parent", (("name", "=", "Renamed parent B"),)),
                        recovery_reader.lookups,
                    )
                    self.assertIn(
                        (
                            "x.scope_record",
                            (("default_code", "=", "SAME"), ("parent_id", "=", 902)),
                        ),
                        recovery_reader.lookups,
                    )

    def test_changed_alias_requires_new_review_even_when_parent_ids_and_counts_match(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = _fixture(major, mode)
                    result = _compile(fixture)
                    fixture.choices = (
                        fixture.choices[0],
                        replace(
                            fixture.choices[1],
                            identity_aliases=(
                                ValueMapping("Parent A", "Another parent A"),
                                ALIASES[1],
                            ),
                        ),
                    )
                    fixture.targets["x.scope_parent"] = (
                        replace(
                            fixture.targets["x.scope_parent"][0],
                            values={"name": "Another parent A"},
                        ),
                        fixture.targets["x.scope_parent"][1],
                    )
                    fresh = saved._match(fixture, result.plans)
                    self.assertEqual(
                        tuple(
                            (
                                row.destination_existing_key_count,
                                row.destination_create_key_count,
                            )
                            for row in fresh.model_matches
                        ),
                        tuple(
                            (
                                row.destination_existing_key_count,
                                row.destination_create_key_count,
                            )
                            for row in result.match.model_matches
                        ),
                    )
                    report = TransferPreflightService().build(
                        result.approved,
                        result.package,
                        result.approval,
                        result.match,
                        fresh,
                        recorded_by=LOCAL_ACTOR.identity,
                        restore_plans=result.plans,
                    )
                    self.assertFalse(report.ready)
                    self.assertIn(
                        "DESTINATION_IDENTITY_ALIAS_DRIFT",
                        tuple(
                            code
                            for dataset in report.datasets
                            for code in dataset.blocker_codes
                        ),
                    )

    def test_fresh_restore_rebinds_alias_evidence_with_independent_keys_and_target_ids(
        self,
    ):
        with TemporaryDirectory() as first_root, TemporaryDirectory() as second_root:
            project_id = str(uuid4())
            first = _fixture(19, "REMOTE")
            first_result = _compile(first)
            first_store = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(first_root, MemorySecretStore())
            )
            stored = first_store.put(project_id, first_result.match)
            self.assertIsNotNone(first_store.read(project_id, stored))
            other_keys = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(first_root, MemorySecretStore())
            )
            with self.assertRaisesRegex(WorkspaceError, "unavailable or changed"):
                other_keys.read(project_id, stored)
            second = _fixture(19, "REMOTE", target_offset=10_000)
            second_result = _compile(second)
            with self.assertRaisesRegex(WorkspaceError, "unavailable or changed"):
                first_store.read(
                    project_id,
                    replace(stored, workspace_id=second.workspace.workspace_id),
                )
            second_store = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(second_root, MemorySecretStore())
            )
            new_stored = second_store.put(project_id, second_result.match)
            self.assertIsNotNone(second_store.read(project_id, new_stored))
            self.assertNotEqual(
                stored.identity_alias_evidence_id, new_stored.identity_alias_evidence_id
            )
            self.assertNotEqual(
                first.workspace.saved_restore_generation_id,
                second.workspace.saved_restore_generation_id,
            )
            self.assertEqual(
                _verification(
                    second,
                    second_result.snapshot,
                    _Readback(second, second_result.snapshot),
                ).status,
                ReconciliationRunStatus.VERIFIED,
            )
            self.assertTrue(
                all(
                    b"Renamed parent" not in path.read_bytes()
                    for path in Path(second_root).rglob("*.ipe")
                )
            )


if __name__ == "__main__":
    unittest.main()
