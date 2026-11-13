"""Exercise portable parent scope through matching, approval and execution."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.application.workspace.execution.service import (
    ExecutionService,
    execution_api_scope,
)
from impodo.application.workspace.execution.reconciliation import ReconciliationService
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_readback import ReadbackRecord
from impodo.domain.odoo.contracts import (
    MetadataSnapshot,
    RecordSnapshot,
    record_snapshot_payload,
)
from impodo.domain.odoo_provenance import OdooOriginBatch, OdooRelationshipOriginColumn
from impodo.domain.preparation.source import SourceRow
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.shared.models import (
    BusinessReference,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.domain.workspace.contracts import SchemaModel
from impodo.domain.workspace.destination_matching import DestinationMatchPlan, RetiredDestinationMatchPlanError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.odoo_relationship_compilation import (
    compile_odoo_relationship_datasets,
)
from impodo.domain.workspace.transfer_review import (
    TransferReviewApproval,
    TransferReviewPackage,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.test_destination_matching import (
    BINDING_HASH,
    HASH,
    _identity,
    _relation,
    _selection,
    _source_schema,
    _workspace,
)


class ScopedTransferFixture:
    """Small fictional two-parent fixture shared with route-level regressions."""

    def __init__(self, major=19, mode="REMOTE"):
        self.now = datetime.now(UTC)
        workspace = _workspace(self.now)
        self.workspace = replace(
            workspace,
            destination_odoo_connection_mode=OdooConnectionMode(mode),
            destination_verified_odoo_version=f"{major}.0",
            destination_verified_target_hash=target_identity_hash(
                connection_mode=mode,
                base_url=workspace.destination_odoo_base_url,
                database=workspace.destination_odoo_database,
            ),
        )
        selection = _selection(self.now)
        child, parent = selection.datasets
        self.child = replace(
            child,
            name="Fictional records",
            source=replace(child.source, model="x.scope_record"),
        )
        self.parent = replace(
            parent,
            name="Fictional parents",
            source=replace(parent.source, model="x.scope_parent"),
        )
        self.selection = replace(selection, datasets=(self.child, self.parent))
        schema = _source_schema(self.workspace, self.now)
        child_model, parent_model = schema.models
        relation = replace(
            _relation(
                "parent_id", "Parent", "many2one", "x.scope_parent", required=True
            ),
            stored=True,
            computed=False,
        )
        self.schema = replace(
            schema,
            odoo_version=f"{major}.0",
            models=(
                SchemaModel(
                    "x.scope_record",
                    "Fictional record",
                    (*child_model.fields, relation),
                ),
                SchemaModel("x.scope_parent", "Fictional parent", parent_model.fields),
            ),
        )
        self.rows = {
            self.child.dataset_id: (
                SourceRow(1, {"default_code": "SAME", "name": "First"}),
                SourceRow(2, {"default_code": "SAME", "name": "Second"}),
            ),
            self.parent.dataset_id: (
                SourceRow(1, {"name": "Parent A"}),
                SourceRow(2, {"name": "Parent B"}),
            ),
        }
        self.origins = {
            self.child.dataset_id: (
                OdooOriginBatch(
                    1,
                    (101, 102),
                    (self.now, self.now),
                    (
                        OdooRelationshipOriginColumn(
                            "parent_id", "many2one", "x.scope_parent", ((7,), (8,))
                        ),
                    ),
                ),
            ),
            self.parent.dataset_id: (OdooOriginBatch(1, (7, 8), (self.now, self.now)),),
        }
        self.fingerprint = TargetFingerprint(
            self.workspace.destination_verified_target_hash,
            mode,
            "destination",
            f"{major}.0",
            self.now.isoformat(),
        )
        self.metadata = MetadataSnapshot(
            self.fingerprint,
            {
                "x.scope_record": ModelMetadata(
                    "x.scope_record",
                    "Fictional record",
                    {
                        "default_code": FieldMetadata("default_code", "char", "Code"),
                        "name": FieldMetadata("name", "char", "Name"),
                        "parent_id": FieldMetadata(
                            "parent_id",
                            "many2one",
                            "Parent",
                            required=True,
                            relation="x.scope_parent",
                        ),
                    },
                ),
                "x.scope_parent": ModelMetadata(
                    "x.scope_parent",
                    "Fictional parent",
                    {"name": FieldMetadata("name", "char", "Name")},
                ),
            },
        )
        self.targets = {
            "x.scope_parent": (
                TargetRecord("x.scope_parent", 901, {"name": "Parent A"}),
                TargetRecord("x.scope_parent", 902, {"name": "Parent B"}),
            ),
            "x.scope_record": (
                TargetRecord(
                    "x.scope_record", 1001, {"default_code": "SAME", "parent_id": 901}
                ),
            ),
        }
        self.identity = replace(
            _identity(self.workspace),
            readable_models=("x.scope_parent", "x.scope_record"),
        )
        self.choices = (
            DestinationMatchKeyChoice(
                self.child.dataset_id, "product-code", scope_fields=("parent_id",)
            ),
            DestinationMatchKeyChoice(self.parent.dataset_id, "uom-name"),
        )
        self.calls = []
        self.captured = []

    def require_current_destination(self, workspace_id, actor):
        del actor
        if (
            workspace_id != self.workspace.workspace_id
            or not self.workspace.destination_database_binding_current
        ):
            raise WorkspaceError("The fixture destination database binding is not current")

    def source_identity_counts(self, workspace_id, dataset_id, key, **kwargs):
        return tuple(
            Counter(
                row[0]
                for row in self.source_key_tuples(workspace_id, dataset_id, (key,))
            ).items()
        )

    def source_key_tuples(self, workspace_id, dataset_id, keys):
        dataset = next(
            item for item in self.selection.datasets if item.dataset_id == dataset_id
        )
        names = {column.stable_key: column.source_name for column in dataset.columns}
        return tuple(
            tuple(row.values.get(names[key]) for key in keys)
            for row in self.rows[dataset_id]
        )

    def source_key_rows(self, workspace_id, dataset_id, key):
        return tuple(
            row[0] for row in self.source_key_tuples(workspace_id, dataset_id, (key,))
        )

    def reader(self, workspace, key, metadata_requests, record_requests):
        self.calls.append(record_requests)
        requested = {request.model: request.fields for request in record_requests}
        return self.metadata, RecordSnapshot(
            self.fingerprint,
            {model: self.targets[model] for model in requested},
            requested,
        )

    def match(self):
        self.captured.clear()
        self.match_plan = DestinationMatchingService(self).check(
            self.workspace,
            self.selection,
            self.schema,
            self.choices,
            api_key="fixture-key",
            credential_binding_hash=BINDING_HASH,
            read_identity=self.identity,
            reader=self.reader,
            recorded_by="Data manager",
            source_origins=self.origins,
            record_snapshot_sink=self.captured.append,
        )
        self.records = self.captured[0]
        return self.match_plan

    def approve(self):
        match = self.match()
        matched = replace(self.workspace, destination_match_plan=match)
        order = TransferOrderService().build(matched, match, recorded_by="Data manager")
        ordered = replace(matched, transfer_order_plan=order)
        self.package = TransferReviewService().build(
            ordered,
            match,
            order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
            model_policies={"x.scope_parent": "reuse_only"},
        )
        self.approval = TransferReviewApproval.approve(
            self.package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=self.now,
        )
        self.approved = replace(
            ordered,
            transfer_review_package=self.package,
            transfer_review_approval=self.approval,
        )
        self.report = TransferPreflightService().build(
            self.approved,
            self.package,
            self.approval,
            match,
            match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.workspace = replace(self.approved, transfer_preflight_report=self.report)
        return self.package

    def compile(self):
        self.approve()
        return compile_transfer_execution_snapshot(
            self.workspace,
            self.selection,
            self.schema,
            self.package,
            self.report,
            self.match_plan,
            self.records,
            source_rows=self.rows,
            source_origins=self.origins,
            source_snapshots={
                item.dataset_id: SimpleNamespace(content_hash=HASH)
                for item in self.selection.datasets
            },
            source_manifest_hashes={
                item.dataset_id: HASH for item in self.selection.datasets
            },
        )


class ScopedOdooTransferTests(TestCase):
    def test_repeated_scalar_keys_match_within_portable_parents_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = ScopedTransferFixture(major, mode)
                    snapshot = fixture.compile()
                    child = next(
                        item
                        for item in fixture.match_plan.model_matches
                        if item.model == "x.scope_record"
                    )
                    self.assertEqual(
                        (
                            child.source_duplicate_key_count,
                            child.destination_existing_key_count,
                            child.destination_create_key_count,
                        ),
                        (0, 1, 1),
                    )
                    self.assertEqual(child.scope_fields, ("parent_id",))
                    self.assertEqual(len(fixture.calls), 2)
                    self.assertEqual(
                        tuple(request.model for request in fixture.calls[0]),
                        ("x.scope_parent",),
                    )
                    self.assertIn("parent_id", fixture.calls[1][0].fields)
                    self.assertIn(("parent_id", "=", 902), fixture.calls[1][0].domain)
                    self.assertEqual(len(fixture.captured), 1)
                    self.assertEqual(
                        set(fixture.records.requested_fields),
                        {"x.scope_record", "x.scope_parent"},
                    )
                    self.assertEqual(
                        fixture.match_plan.destination_record_snapshot_hash,
                        content_hash(record_snapshot_payload(fixture.records)),
                    )
                    self.assertEqual(
                        DestinationMatchPlan.from_json(fixture.match_plan.to_json()),
                        fixture.match_plan,
                    )
                    self.assertEqual(
                        TransferReviewPackage.from_json(fixture.package.to_json()),
                        fixture.package,
                    )
                    children = tuple(
                        row
                        for row in snapshot.rows
                        if row.target_model == "x.scope_record"
                    )
                    self.assertEqual(
                        tuple(row.disposition for row in children), ("UPDATE", "CREATE")
                    )
                    self.assertNotEqual(
                        children[0].source_identity, children[1].source_identity
                    )
                    self.assertEqual(
                        children[0].business_scope,
                        (
                            BusinessReference(
                                "x.scope_parent", ("Parent A",), target_fields=("name",)
                            ),
                        ),
                    )
                    self.assertEqual(children[1].business_scope[0].key, ("Parent B",))
                    self.assertEqual(
                        tuple(
                            scope.key
                            for row in children
                            for scope in row.business_scope
                        ),
                        (("Parent A",), ("Parent B",)),
                    )
                    mappings = compile_odoo_relationship_datasets(fixture.match_plan)
                    mapped = next(
                        item
                        for item in mappings
                        if item.target_model == "x.scope_record"
                    )
                    self.assertEqual(
                        mapped.target_scope[0].target_fields, ("parent_id",)
                    )
                    self.assertEqual(
                        mapped.target_scope[0].resolver.model, "x.scope_parent"
                    )
                    self.assertIn(
                        "x.scope_parent",
                        {item.model for item in execution_api_scope(snapshot).models},
                    )

    def test_duplicate_within_one_parent_blocks_but_equal_key_under_another_parent_does_not(
        self,
    ):
        fixture = ScopedTransferFixture()
        origin = fixture.origins[fixture.child.dataset_id][0]
        fixture.origins[fixture.child.dataset_id] = (
            replace(
                origin,
                relationships=(replace(origin.relationships[0], values=((7,), (7,))),),
            ),
        )
        plan = fixture.match()
        child = next(
            item for item in plan.model_matches if item.model == "x.scope_record"
        )
        self.assertEqual(child.source_duplicate_key_count, 1)
        self.assertFalse(plan.ready)

    def test_missing_or_ambiguous_existing_parent_prevents_child_reads(self):
        for targets in (
            (),
            (
                TargetRecord("x.scope_parent", 901, {"name": "Parent A"}),
                TargetRecord("x.scope_parent", 902, {"name": "Parent A"}),
            ),
        ):
            fixture = ScopedTransferFixture()
            fixture.targets["x.scope_parent"] = targets
            with self.assertRaisesRegex(
                WorkspaceError, "parent must already match exactly one"
            ):
                fixture.match()
            self.assertEqual(len(fixture.calls), 1)
            self.assertEqual(fixture.captured, [])

    def test_blank_or_missing_protected_scope_fails_before_child_reads(self):
        for missing in (True, False):
            fixture = ScopedTransferFixture()
            origin = fixture.origins[fixture.child.dataset_id][0]
            fixture.origins[fixture.child.dataset_id] = (
                replace(
                    origin,
                    relationships=()
                    if missing
                    else (replace(origin.relationships[0], values=((7,), ())),),
                ),
            )
            with self.assertRaisesRegex(WorkspaceError, "scope"):
                fixture.match()
            self.assertEqual(len(fixture.calls), 1)

    def test_create_absence_check_resolves_only_exact_existing_parent(self):
        fixture = ScopedTransferFixture()
        snapshot = fixture.compile()
        calls = []

        class Writer:
            def find_ids_many(self, model, domains):
                calls.append((model, domains))
                return tuple(
                    (902,) if model == "x.scope_parent" else () for _ in domains
                )

        ExecutionService._assert_create_rows_still_absent(
            snapshot.rows, {item.dataset: item for item in snapshot.datasets}, Writer()
        )
        self.assertEqual(calls[0], ("x.scope_parent", ((("name", "=", "Parent B"),),)))
        self.assertEqual(
            calls[1],
            (
                "x.scope_record",
                ((("default_code", "=", "SAME"), ("parent_id", "=", 902)),),
            ),
        )

    def test_parent_retargeting_invalidates_preflight_even_when_counts_match(self):
        fixture = ScopedTransferFixture()
        fixture.approve()
        original = fixture.match_plan
        fixture.targets["x.scope_parent"] = (
            TargetRecord("x.scope_parent", 990, {"name": "Parent A"}),
            TargetRecord("x.scope_parent", 902, {"name": "Parent B"}),
        )
        fresh = fixture.match()
        report = TransferPreflightService().build(
            fixture.approved,
            fixture.package,
            fixture.approval,
            original,
            fresh,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.assertFalse(report.ready)
        self.assertTrue(
            any(
                "DESTINATION_RECORD_IDENTITY_DRIFT" in item.blocker_codes
                for item in report.datasets
            )
        )

    def test_repeat_matching_creates_nothing_and_target_duplicates_remain_blocking(
        self,
    ):
        fixture = ScopedTransferFixture()
        fixture.targets["x.scope_record"] += (
            TargetRecord(
                "x.scope_record",
                1002,
                {"default_code": "SAME", "parent_id": [902, "Parent B"]},
            ),
        )
        plan = fixture.match()
        child = next(item for item in plan.model_matches if item.scope_fields)
        self.assertEqual(
            (child.destination_existing_key_count, child.destination_create_key_count),
            (2, 0),
        )
        fixture.targets["x.scope_record"] += (
            TargetRecord(
                "x.scope_record", 1003, {"default_code": "SAME", "parent_id": 902}
            ),
        )
        self.assertFalse(fixture.match().ready)

    def test_source_numeric_ids_do_not_enter_scoped_portable_identity(self):
        fixture = ScopedTransferFixture()
        original = fixture.compile()
        owner = fixture.origins[fixture.child.dataset_id][0]
        parent = fixture.origins[fixture.parent.dataset_id][0]
        fixture.origins[fixture.child.dataset_id] = (
            replace(
                owner,
                odoo_ids=(301, 302),
                relationships=(replace(owner.relationships[0], values=((70,), (80,))),),
            ),
        )
        fixture.origins[fixture.parent.dataset_id] = (
            replace(parent, odoo_ids=(70, 80)),
        )
        changed = fixture.compile()
        self.assertEqual(
            tuple(
                (
                    row.source_identity,
                    row.business_identity,
                    row.business_scope,
                    row.proposed_external_id,
                )
                for row in original.rows
            ),
            tuple(
                (
                    row.source_identity,
                    row.business_identity,
                    row.business_scope,
                    row.proposed_external_id,
                )
                for row in changed.rows
            ),
        )

    def test_second_read_rejects_unsupported_or_mismatched_metadata_and_record_versions(
        self,
    ):
        for changed_part in (0, 1):
            for version in ("20.0", "19.garbage", "21.0"):
                with self.subTest(part=changed_part, version=version):
                    fixture = ScopedTransferFixture()
                    base_reader = fixture.reader

                    def changed_reader(*args):
                        pair = list(base_reader(*args))
                        if len(fixture.calls) == 2:
                            pair[changed_part] = replace(
                                pair[changed_part],
                                fingerprint=replace(
                                    pair[changed_part].fingerprint, odoo_version=version
                                ),
                            )
                        return tuple(pair)

                    fixture.reader = changed_reader
                    with self.assertRaisesRegex(
                        WorkspaceError, "unsupported or different Odoo target"
                    ):
                        fixture.match()
                    self.assertEqual(fixture.captured, [])

    def test_retired_scalar_contracts_require_new_destination_check(self):
        fixture = ScopedTransferFixture()
        match = fixture.match()
        for retired_version in (16, 17):
            with self.subTest(retired_version=retired_version):
                with self.assertRaisesRegex(RetiredDestinationMatchPlanError, "Check the destination again"):
                    replace(match, contract_version=retired_version)
                with self.assertRaisesRegex(RetiredDestinationMatchPlanError, "frozen source data is unchanged"):
                    DestinationMatchPlan.from_json(json.dumps({"contract_version": retired_version}))
        self.assertEqual(DestinationMatchPlan.from_json(match.to_json()), match)


    def test_interrupted_child_create_is_recovered_by_exact_parent_scope(self):
        fixture = ScopedTransferFixture()
        snapshot = fixture.compile()
        writes = tuple(
            row for row in snapshot.rows if row.disposition in {"CREATE", "UPDATE"}
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
            status=ExecutionRunStatus.RUNNING,
            started_at=fixture.now,
            started_by="Data manager",
            completed_at=None,
            rows=tuple(
                ExecutionRowAttempt(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    target_model=row.target_model,
                    operation=row.disposition,
                    field_names=tuple(intent.field for intent in row.fields),
                    proposed_external_id=row.proposed_external_id,
                    status=ExecutionRowStatus.COMMITTED
                    if row.disposition == "UPDATE"
                    else ExecutionRowStatus.IN_FLIGHT,
                    attempt=1,
                    odoo_id=1001 if row.disposition == "UPDATE" else None,
                )
                for row in writes
            ),
        )
        lookups = []

        class Reader:
            target_hash = snapshot.target_hash
            scope_hash = execution_api_scope(snapshot).semantic_hash
            imports_external_ids = True

            def find_records_many(self, model, queries):
                lookups.extend((model, query.domain) for query in queries)
                result = []
                for query in queries:
                    if model == "x.scope_parent":
                        matches = tuple(
                            ReadbackRecord(record.odoo_id, record.values)
                            for record in fixture.targets[model]
                            if all(
                                record.values.get(field) == value
                                for field, _, value in query.domain
                            )
                        )
                    else:
                        matches = ()
                    result.append(matches)
                return tuple(result)

            def read_ids(self, model, identifiers, fields):
                return tuple(
                    ReadbackRecord(
                        identifier,
                        {"default_code": "SAME", "name": "First", "parent_id": 901},
                    )
                    for identifier in identifiers
                    if model == "x.scope_record" and identifier == 1001
                )

            def read_external_ids(self, identifiers):
                return ()

        service = ReconciliationService(
            SimpleNamespace(execution_snapshot=lambda *args: snapshot),
            SimpleNamespace(
                get_run=lambda *args: run, get_current_run=lambda *args: run
            ),
            SimpleNamespace(),
            CapabilityAuthorizationPolicy(),
            database_admission=fixture.require_current_destination,
        )
        report = service.assess_recovery(
            snapshot.workspace_id,
            expected_execution_run_id=run.run_id,
            reader=Reader(),
            actor=LOCAL_ACTOR,
        )
        recovered = next(row for row in report.rows if row.row_id == writes[1].row_id)
        self.assertTrue(recovered.retry_safe)
        self.assertIn(
            (
                "x.scope_record",
                (("default_code", "=", "SAME"), ("parent_id", "=", 902)),
            ),
            lookups,
        )

    def test_recursive_and_inbound_scoped_relationships_stop_before_destination_reads(
        self,
    ):
        for recursive in (False, True):
            fixture = ScopedTransferFixture()
            child, parent = fixture.schema.models
            parent = replace(
                parent,
                fields=(
                    *parent.fields,
                    replace(
                        _relation("record_id", "Record", "many2one", "x.scope_record"),
                        stored=True,
                        computed=False,
                    ),
                ),
            )
            fixture.schema = replace(fixture.schema, models=(child, parent))
            if recursive:
                fixture.choices = (
                    fixture.choices[0],
                    replace(fixture.choices[1], scope_fields=("record_id",)),
                )
            with self.assertRaisesRegex(
                WorkspaceError, "unscoped parent|Links targeting"
            ):
                fixture.match()
            self.assertEqual(fixture.calls, [])

    def test_scalar_duplicate_exclusions_cannot_silently_discard_a_valid_scoped_peer(
        self,
    ):
        fixture = ScopedTransferFixture()
        with self.assertRaisesRegex(
            WorkspaceError, "Putting scoped matching issues aside"
        ):
            DestinationMatchingService(fixture).check(
                fixture.workspace,
                fixture.selection,
                fixture.schema,
                fixture.choices,
                api_key="fixture-key",
                credential_binding_hash=BINDING_HASH,
                read_identity=fixture.identity,
                reader=fixture.reader,
                recorded_by="Data manager",
                source_origins=fixture.origins,
                excluded_source_rows={fixture.child.dataset_id: (1,)},
            )
        self.assertEqual(fixture.calls, [])
