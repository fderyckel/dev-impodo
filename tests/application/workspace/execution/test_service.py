from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.workspace.execution.service import (
    ExecutionService,
    _execution_blocker_summary,
    _execution_dependency_summary,
    execution_api_scope,
)
from impodo.domain.execution.models import (
    ExecutionRowStatus,
    ExecutionRunStatus,
)
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    RelationshipBlocker,
    RelationshipComponent,
    plan_execution_rows,
)
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    OdooReadIdentity,
    OdooWriteIdentity,
    target_record_binding_hash,
)
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.adapters.odoo.connectors import Json2Config
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode
from impodo.web.composition.target_writers import _write_executor
from impodo.domain.workspace.errors import WorkspaceError


HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64


def _row(
    *,
    dataset: str,
    model: str,
    source_row: int,
    source_identity: tuple[str, ...],
    business_identity: tuple[str, ...],
    disposition: str,
    fields: tuple[FieldIntent, ...],
) -> ExecutionRow:
    return ExecutionRow(
        row_id="sha256:" + f"{source_row:064x}",
        dataset=dataset,
        source_row=source_row,
        source_trace_id="sha256:" + f"{source_row + 100:064x}",
        source_identity=source_identity,
        target_model=model,
        business_identity=business_identity,
        business_scope=(),
        disposition=disposition,
        target_match_count=0 if disposition == "CREATE" else 1,
        proposed_external_id=(
            f"impodo_test.{dataset}_{source_row}" if disposition == "CREATE" else ""
        ),
        fields=fields,
        row_hash="sha256:" + f"{source_row + 200:064x}",
    )


def _snapshot() -> ExecutionSnapshot:
    category = _row(
        dataset="categories",
        model="product.category",
        source_row=2,
        source_identity=("CAT",),
        business_identity=("Category",),
        disposition="CREATE",
        fields=(FieldIntent("name", "SET_VALUE", "Category"),),
    )
    product = _row(
        dataset="products",
        model="product.template",
        source_row=3,
        source_identity=("P1",),
        business_identity=("P1",),
        disposition="CREATE",
        fields=(
            FieldIntent("default_code", "SET_VALUE", "P1"),
            FieldIntent("name", "SET_VALUE", "Product"),
            FieldIntent(
                "categ_id",
                "SET_VALUE",
                LogicalReference(
                    origin="incoming",
                    key=("CAT",),
                    dataset="categories",
                ),
                kind="relation",
                relation_operation="replace",
                related_model="product.category",
                related_identity_fields=("name",),
                dependency_strength="hard",
            ),
        ),
    )
    contact = _row(
        dataset="contacts",
        model="res.partner",
        source_row=4,
        source_identity=("C1",),
        business_identity=("C1",),
        disposition="UPDATE",
        fields=(FieldIntent("email", "SET_VALUE", "new@example.test"),),
    )
    snapshot = ExecutionSnapshot(
        workspace_id=str(uuid4()),
        preflight_run_id=str(uuid4()),
        mapping_id=str(uuid4()),
        mapping_version=1,
        mapping_content_hash=HASH,
        compiled_plan_hash=HASH,
        staging_run_id=str(uuid4()),
        staging_content_hash=HASH,
        quality_run_id=str(uuid4()),
        quality_content_hash=HASH,
        normalization_run_id=str(uuid4()),
        normalization_content_hash=HASH,
        normalization_lifecycle_version=1,
        eligible_dataset_hash=HASH,
        frozen_input_hash=HASH,
        preflight_result_hash=HASH,
        metadata_snapshot_hash=HASH,
        record_snapshot_hash=HASH,
        target_hash=TARGET_HASH,
        target_database="odoo19_disposable",
        target_odoo_version="19.0",
        target_snapshot_at="2026-08-06T12:00:00Z",
        target_module_versions={"base": "19.0.1.0"},
        datasets=(
            ExecutionDataset(
                "categories",
                "product.category",
                0,
                (),
                "update",
                ("name",),
                (),
            ),
            ExecutionDataset(
                "products",
                "product.template",
                1,
                ("categories",),
                "update",
                ("default_code",),
                (),
            ),
            ExecutionDataset(
                "contacts",
                "res.partner",
                2,
                (),
                "update",
                ("ref",),
                (),
            ),
        ),
        counts={
            "CREATE": 2,
            "UPDATE": 1,
            "UNCHANGED": 0,
            "AMBIGUOUS": 0,
            "BLOCKED": 0,
        },
        rows=(category, product, contact),
        root_hash=HASH,
    )
    return _with_plan(snapshot)


def _with_plan(snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
    rows_with_strength = tuple(
        replace(
            row,
            fields=tuple(
                replace(
                    intent,
                    dependency_strength=(
                        "deferrable" if intent.defer_on_create else "hard"
                    ),
                )
                if (
                    intent.kind == "relation"
                    and not intent.dependency_strength
                    and any(
                        isinstance(value, LogicalReference)
                        and value.origin == "incoming"
                        for value in (
                            intent.value
                            if isinstance(intent.value, tuple)
                            else (intent.value,)
                        )
                    )
                )
                else intent
                for intent in row.fields
            ),
        )
        for row in snapshot.rows
    )
    rows, relationship_plan = plan_execution_rows(
        rows_with_strength,
        snapshot.datasets,
    )
    return replace(
        snapshot,
        rows=rows,
        relationship_plan=relationship_plan,
    )


def _remote_many2many_snapshot() -> ExecutionSnapshot:
    snapshot = _snapshot()
    product = replace(
        snapshot.rows[1],
        fields=(
            *snapshot.rows[1].fields[:-1],
            FieldIntent(
                "x_category_ids",
                "SET_VALUE",
                (
                    LogicalReference(
                        origin="incoming",
                        key=("CAT",),
                        dataset="categories",
                    ),
                    BusinessReference(
                        "product.category",
                        ("Existing Category",),
                    ),
                ),
                kind="relation",
                relation_operation="replace",
                related_model="product.category",
                related_identity_fields=("name",),
            ),
        ),
    )
    return _with_plan(
        replace(
            snapshot,
            datasets=snapshot.datasets[:2],
            rows=(snapshot.rows[0], product),
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
    )


def _generated_variant_snapshot() -> ExecutionSnapshot:
    snapshot = _snapshot()
    product = _row(
        dataset="products",
        model="product.template",
        source_row=20,
        source_identity=("P1",),
        business_identity=("P1",),
        disposition="CREATE",
        fields=(FieldIntent("default_code", "SET_VALUE", "P1"),),
    )
    component = _row(
        dataset="bom_lines",
        model="mrp.bom.line",
        source_row=21,
        source_identity=("BOM1", "P1"),
        business_identity=("BOM1", "P1"),
        disposition="CREATE",
        fields=(
            FieldIntent(
                "product_id",
                "SET_VALUE",
                LogicalReference(
                    origin="incoming",
                    key=("P1",),
                    dataset="products",
                ),
                kind="relation",
                relation_operation="replace",
                related_model="product.product",
                related_identity_fields=("default_code",),
                dependency_strength="hard",
                incoming_projection_field="product_variant_id",
            ),
        ),
    )
    return _with_plan(
        replace(
            snapshot,
            datasets=(
                ExecutionDataset(
                    "products",
                    "product.template",
                    0,
                    (),
                    "update",
                    ("default_code",),
                    (),
                ),
                ExecutionDataset(
                    "bom_lines",
                    "mrp.bom.line",
                    1,
                    ("products",),
                    "update",
                    ("bom_id", "product_id"),
                    (),
                ),
            ),
            rows=(product, component),
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
    )


def _remote_relation_update_snapshot(*, many2many: bool) -> ExecutionSnapshot:
    snapshot = _snapshot()
    relation = (
        FieldIntent(
            "x_tag_ids",
            "SET_VALUE",
            (
                BusinessReference("x.tag", ("BLUE",)),
                BusinessReference("x.tag", ("FOOD",)),
            ),
            kind="relation",
            relation_operation="replace",
            related_model="x.tag",
            related_identity_fields=("code",),
        )
        if many2many
        else FieldIntent(
            "parent_id",
            "SET_VALUE",
            BusinessReference("res.partner", ("PARENT",)),
            kind="relation",
            relation_operation="replace",
            related_model="res.partner",
            related_identity_fields=("ref",),
        )
    )
    contact = replace(snapshot.rows[-1], fields=(relation,))
    return _with_plan(
        replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=(contact,),
            counts={
                "CREATE": 0,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
    )


def _remote_cycle_snapshot(*, required_at_create: bool = False) -> ExecutionSnapshot:
    snapshot = _snapshot()
    first = _row(
        dataset="first_nodes",
        model="x.first.node",
        source_row=20,
        source_identity=("FIRST",),
        business_identity=("FIRST",),
        disposition="CREATE",
        fields=(
            FieldIntent("code", "SET_VALUE", "FIRST"),
            FieldIntent(
                "second_id",
                "SET_VALUE",
                LogicalReference(
                    origin="incoming",
                    key=("SECOND",),
                    dataset="second_nodes",
                ),
                kind="relation",
                relation_operation="replace",
                related_model="x.second.node",
                related_identity_fields=("code",),
                dependency_strength=("hard" if required_at_create else "deferrable"),
                defer_on_create=not required_at_create,
            ),
        ),
    )
    second = _row(
        dataset="second_nodes",
        model="x.second.node",
        source_row=21,
        source_identity=("SECOND",),
        business_identity=("SECOND",),
        disposition="CREATE",
        fields=(
            FieldIntent("code", "SET_VALUE", "SECOND"),
            FieldIntent(
                "first_id",
                "SET_VALUE",
                LogicalReference(
                    origin="incoming",
                    key=("FIRST",),
                    dataset="first_nodes",
                ),
                kind="relation",
                relation_operation="replace",
                related_model="x.first.node",
                related_identity_fields=("code",),
                dependency_strength=("hard" if required_at_create else "deferrable"),
                defer_on_create=not required_at_create,
            ),
        ),
    )
    return _with_plan(
        replace(
            snapshot,
            datasets=(
                ExecutionDataset(
                    "first_nodes",
                    "x.first.node",
                    0,
                    ("second_nodes",),
                    "update",
                    ("code",),
                    (),
                ),
                ExecutionDataset(
                    "second_nodes",
                    "x.second.node",
                    1,
                    ("first_nodes",),
                    "update",
                    ("code",),
                    (),
                ),
            ),
            rows=(first, second),
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
    )


class _Journal:
    def __init__(self) -> None:
        self.run = None
        self.rows = {}

    def get_current_run(self, project_id, snapshot_hash=None):
        del project_id, snapshot_hash
        return self.run

    def get_run(self, project_id, run_id):
        del project_id
        return self.run if self.run and self.run.run_id == run_id else None

    def start_run(self, project_id, run, *, actor):
        del project_id, actor
        self.run = run
        self.rows = {item.row_id: item for item in run.rows}

    def record_outcomes(self, project_id, run_id, rows):
        del project_id, run_id
        self.rows.update({item.row_id: item for item in rows})
        self.run = replace(
            self.run,
            rows=tuple(self.rows[item.row_id] for item in self.run.rows),
        )

    def record_batch_started(self, project_id, run_id, rows):
        del project_id, run_id
        self.rows.update({item.row_id: item for item in rows})
        self.run = replace(
            self.run,
            rows=tuple(self.rows[item.row_id] for item in self.run.rows),
        )

    def record_recovery(self, project_id, run_id, rows, *, actor):
        del project_id, run_id, actor
        self.rows.update({item.row_id: item for item in rows})
        self.run = replace(
            self.run,
            rows=tuple(self.rows[item.row_id] for item in self.run.rows),
        )

    def finish_run(self, project_id, run_id, status, *, actor):
        del project_id, run_id, actor
        self.run = replace(
            self.run,
            status=status,
            completed_at=datetime.now(timezone.utc),
            rows=tuple(self.rows[item.row_id] for item in self.run.rows),
        )
        return self.run


class _Executor:
    target_hash = TARGET_HASH

    def __init__(
        self,
        scope_hash: str,
        *,
        unknown=False,
        update_error: Exception | None = None,
        lookup_ids: tuple[int, ...] = (50,),
        lookup_results: dict[tuple[object, ...], tuple[int, ...]] | None = None,
    ) -> None:
        self.scope_hash = scope_hash
        self.unknown = unknown
        self.update_error = update_error
        self.lookup_ids = lookup_ids
        self.lookup_results = lookup_results or {}
        self.lookups = []
        self.bulk_lookups = []
        self.single_lookup_count = 0
        self.creates = []
        self.loads = []
        self.updates = []
        self.projection_reads = []
        self.next_id = 10

    def find_ids(self, model, domain):
        self.single_lookup_count += 1
        self.lookup = (model, tuple(domain))
        self.lookups.append(self.lookup)
        return self.lookup_results.get(self.lookup, self.lookup_ids)

    def find_ids_many(self, model, domains):
        page = tuple(tuple(domain) for domain in domains)
        self.bulk_lookups.append((model, page))
        results = []
        for domain in page:
            self.lookup = (model, domain)
            self.lookups.append(self.lookup)
            results.append(self.lookup_results.get(self.lookup, self.lookup_ids))
        return tuple(results)

    def create_rows(self, model, values):
        if self.unknown:
            raise OdooWriteOutcomeUnknown("lost response")
        rows = tuple(dict(item) for item in values)
        self.creates.append((model, rows))
        ids = tuple(range(self.next_id, self.next_id + len(rows)))
        self.next_id += len(rows)
        return ids

    def load_create_rows(self, model, values, external_ids):
        if self.unknown:
            raise OdooWriteOutcomeUnknown("lost response")
        rows = tuple(dict(item) for item in values)
        self.loads.append((model, rows, tuple(external_ids)))
        ids = tuple(range(self.next_id, self.next_id + len(rows)))
        self.next_id += len(rows)
        return ids

    def update_row(self, model, record_id, values):
        if self.update_error is not None:
            raise self.update_error
        self.updates.append((model, record_id, dict(values)))

    def read_projected_ids(
        self,
        model,
        identifiers,
        projection_field,
        target_model,
    ):
        requested = tuple(identifiers)
        self.projection_reads.append(
            (model, requested, projection_field, target_model)
        )
        return tuple(identifier + 100 for identifier in requested)


class _CrashAfterCheckpoint(_Executor):
    def create_rows(self, model, values):
        del model, values
        raise RuntimeError("simulated process interruption")


class _RejectFirstCreate(_Executor):
    def __init__(self, scope_hash: str) -> None:
        super().__init__(scope_hash)
        self.create_attempts = []

    def create_rows(self, model, values):
        self.create_attempts.append((model, tuple(values)))
        raise OdooWriteRejected("known validation rejection")


class _CrashDuringCompletion(_Executor):
    def update_row(self, model, record_id, values):
        del model, record_id, values
        raise RuntimeError("simulated completion interruption")


class _CrashDuringProjection(_Executor):
    def read_projected_ids(
        self,
        model,
        identifiers,
        projection_field,
        target_model,
    ):
        del model, identifiers, projection_field, target_model
        raise RuntimeError("simulated generated-receipt interruption")


class ExecutionServiceTests(unittest.TestCase):
    def _service(self, snapshot, *, mode=OdooConnectionMode.LOCAL):
        planned = _with_plan(snapshot)
        object.__setattr__(snapshot, "rows", planned.rows)
        object.__setattr__(
            snapshot,
            "relationship_plan",
            planned.relationship_plan,
        )
        journal = _Journal()
        project = SimpleNamespace(
            workspace_id=snapshot.workspace_id,
            odoo_connection_mode=mode,
            source_mode=SourceMode.FILE,
        )
        service = ExecutionService(
            SimpleNamespace(get=lambda _project_id: project),
            SimpleNamespace(current_execution_snapshot=lambda _project_id: snapshot),
            journal,
            CapabilityAuthorizationPolicy(),
        )
        return service, journal

    def test_browser_guidance_bounds_groups_and_record_type_labels(self):
        snapshot = _with_plan(_snapshot())
        extra_row = replace(
            snapshot.rows[0],
            row_id="sha256:" + "9" * 64,
            dataset="bom_component_lines",
        )
        extra_dataset = replace(
            snapshot.datasets[0],
            dataset="bom_component_lines",
            sequence=len(snapshot.datasets),
        )
        rows = (*snapshot.rows, extra_row)
        components = (
            RelationshipComponent(
                sequence=0,
                row_ids=tuple(row.row_id for row in rows),
            ),
            *(
                RelationshipComponent(
                    sequence=sequence,
                    row_ids=(snapshot.rows[0].row_id,),
                )
                for sequence in range(1, 7)
            ),
        )
        blockers = tuple(
            RelationshipBlocker(
                row_id=rows[index % len(rows)].row_id,
                code=f"SYNTHETIC_BLOCKER_{index}",
            )
            for index in range(7)
        )
        bounded = replace(
            snapshot,
            datasets=(*snapshot.datasets, extra_dataset),
            rows=rows,
            relationship_plan=replace(
                snapshot.relationship_plan,
                components=components,
                blockers=blockers,
            ),
        )

        dependency_summary = _execution_dependency_summary(bounded)
        blocker_summary = _execution_blocker_summary(bounded)

        self.assertEqual(len(dependency_summary.groups), 5)
        self.assertEqual(dependency_summary.omitted_group_count, 2)
        self.assertEqual(
            len(dependency_summary.groups[0].dataset_labels),
            3,
        )
        self.assertEqual(
            dependency_summary.groups[0].omitted_dataset_count,
            1,
        )
        self.assertEqual(len(blocker_summary.groups), 5)
        self.assertEqual(blocker_summary.omitted_group_count, 2)

    def test_generated_variant_is_read_in_one_page_and_journalled_before_bom(self):
        snapshot = _generated_variant_snapshot()
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(
            executor.projection_reads,
            [
                (
                    "product.template",
                    (10,),
                    "product_variant_id",
                    "product.product",
                )
            ],
        )
        self.assertEqual(executor.loads[1][1][0]["product_id/.id"], "110")
        product_attempt = next(
            item for item in run.rows if item.dataset == "products"
        )
        self.assertEqual(product_attempt.status, ExecutionRowStatus.COMMITTED)
        self.assertEqual(
            tuple(
                (
                    item.projection_field,
                    item.target_model,
                    item.odoo_id,
                )
                for item in product_attempt.projected_receipts
            ),
            (("product_variant_id", "product.product", 110),),
        )

    def test_changed_confirmation_hash_stops_before_journal_or_target_io(self):
        snapshot = _snapshot()
        service, journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        with self.assertRaisesRegex(WorkspaceError, "preview changed"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash="sha256:" + "9" * 64,
                executor=executor,
                actor=LOCAL_ACTOR,
            )

        self.assertIsNone(journal.run)
        self.assertEqual(executor.creates, [])
        self.assertEqual(executor.updates, [])

    def test_zero_change_comparison_records_completed_run_without_target_io(self):
        current = _snapshot()
        snapshot = replace(
            current,
            rows=tuple(
                replace(row, disposition="UNCHANGED", fields=()) for row in current.rows
            ),
            counts={
                "CREATE": 0,
                "UPDATE": 0,
                "UNCHANGED": len(current.rows),
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, journal = self._service(snapshot)

        preview = service.current_preview(snapshot.workspace_id)
        assert preview is not None
        self.assertTrue(preview.can_complete_without_load)

        run = service.complete_no_changes(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.total_count, 0)
        self.assertEqual(journal.run, run)

    def test_rotated_read_generation_invalidates_remote_load_preview(self):
        snapshot = replace(
            _snapshot(),
            read_credential_binding_hash="sha256:" + "1" * 64,
            read_principal_hash="sha256:" + "2" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            readable_models=(
                "product.category",
                "product.template",
                "res.partner",
            ),
        )
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        service.current_read_credential_binding = lambda _project: "sha256:" + "9" * 64

        preview = service.current_preview(snapshot.workspace_id)

        assert preview is not None
        self.assertFalse(preview.can_load)
        self.assertTrue(preview.credential_refresh_required)
        self.assertIn("read key changed", preview.scope_error)

    def test_remote_load_reprobes_read_acl_before_journalling(self):
        snapshot = replace(
            _snapshot(),
            read_credential_binding_hash="sha256:" + "1" * 64,
            read_principal_hash="sha256:" + "2" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            readable_models=(
                "product.category",
                "product.template",
                "res.partner",
            ),
        )
        service, journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        service.require_remote_read_identity = True
        service.require_remote_write_identity = True
        service.current_read_credential_binding = lambda _project: (
            snapshot.read_credential_binding_hash
        )
        scope = execution_api_scope(snapshot)
        write_identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash="sha256:" + "5" * 64,
            permission_hash="sha256:" + "6" * 64,
            context_hash=snapshot.read_context_hash,
            readable_models=tuple(item.model for item in scope.models),
            writable_models=tuple(
                item.model for item in scope.models if item.write_fields
            ),
            observed_at="2026-08-19T00:00:00Z",
        )
        changed_read_identity = OdooReadIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=snapshot.read_principal_hash,
            permission_hash="sha256:" + "9" * 64,
            context_hash=snapshot.read_context_hash,
            readable_models=snapshot.readable_models,
            observed_at="2026-08-19T00:00:00Z",
        )
        executor = _Executor(scope.semantic_hash)

        with self.assertRaisesRegex(WorkspaceError, "permissions.*changed"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
                read_identity=changed_read_identity,
                read_credential_binding_hash=(snapshot.read_credential_binding_hash),
                write_identity=write_identity,
                write_credential_binding_hash="sha256:" + "7" * 64,
            )

        self.assertIsNone(journal.run)
        self.assertEqual(executor.creates, [])
        self.assertEqual(executor.updates, [])

    def test_loaded_preview_cannot_be_submitted_again(self):
        snapshot = _snapshot()
        service, journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )
        first_creates = tuple(executor.creates)
        first_updates = tuple(executor.updates)

        with self.assertRaisesRegex(WorkspaceError, "already loaded"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
            )

        self.assertIsNotNone(journal.run)
        self.assertEqual(tuple(executor.creates), first_creates)
        self.assertEqual(tuple(executor.updates), first_updates)

    def test_loads_dependency_order_batches_creates_and_updates_unique_match(self):
        snapshot = _snapshot()
        service, _journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        progress = []

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
            progress=progress.append,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.committed_count, 3)
        self.assertEqual(executor.creates[0][0], "product.category")
        self.assertEqual(executor.creates[1][1][0]["categ_id"], 10)
        self.assertEqual(
            executor.updates,
            [("res.partner", 50, {"email": "new@example.test"})],
        )
        self.assertEqual(progress[0].planned_count, 3)
        self.assertEqual(progress[-1].status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(progress[-1].committed_count, 3)
        self.assertEqual(
            [item.total_count - item.planned_count for item in progress],
            sorted(item.total_count - item.planned_count for item in progress),
        )

    def test_stale_dependency_order_is_blocked_before_any_odoo_write(self):
        snapshot = _snapshot()
        stale = replace(
            snapshot,
            datasets=(
                replace(snapshot.datasets[1], sequence=0),
                replace(snapshot.datasets[0], sequence=1),
                snapshot.datasets[2],
            ),
        )
        service, _journal = self._service(stale)

        preview = service.current_preview(stale.workspace_id)

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertFalse(preview.can_load)
        self.assertIn("dependencies are loaded first", preview.scope_error)

    def test_remote_load_journals_exact_write_principal_evidence(self):
        snapshot = _snapshot()
        service, journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        scope = execution_api_scope(snapshot)
        identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash="sha256:" + "3" * 64,
            permission_hash="sha256:" + "4" * 64,
            context_hash="sha256:" + "5" * 64,
            readable_models=tuple(item.model for item in scope.models),
            writable_models=tuple(
                item.model for item in scope.models if item.write_fields
            ),
            observed_at="2026-08-12T00:00:00Z",
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=_Executor(scope.semantic_hash),
            actor=LOCAL_ACTOR,
            write_identity=identity,
            write_credential_binding_hash="sha256:" + "6" * 64,
        )

        self.assertEqual(run.write_principal_hash, identity.principal_hash)
        self.assertEqual(run.write_permission_hash, identity.permission_hash)
        self.assertEqual(run.write_context_hash, identity.context_hash)
        self.assertEqual(
            run.write_credential_binding_hash,
            "sha256:" + "6" * 64,
        )
        self.assertEqual(journal.run, run)

    def test_write_identity_scope_mismatch_stops_before_journal_or_target_io(self):
        snapshot = _snapshot()
        service, journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        scope = execution_api_scope(snapshot)
        identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash="sha256:" + "3" * 64,
            permission_hash="sha256:" + "4" * 64,
            context_hash="sha256:" + "5" * 64,
            readable_models=("res.partner",),
            writable_models=("res.partner",),
            observed_at="2026-08-12T00:00:00Z",
        )

        with self.assertRaisesRegex(WorkspaceError, "read-back scope changed"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=_Executor(scope.semantic_hash),
                actor=LOCAL_ACTOR,
                write_identity=identity,
                write_credential_binding_hash="sha256:" + "6" * 64,
            )

        self.assertIsNone(journal.run)

    def test_composed_remote_service_requires_write_identity_before_journal(self):
        snapshot = _snapshot()
        service, journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        service.require_remote_write_identity = True

        with self.assertRaisesRegex(WorkspaceError, "remote write credential"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=_Executor(execution_api_scope(snapshot).semantic_hash),
                actor=LOCAL_ACTOR,
            )

        self.assertIsNone(journal.run)

    def test_unknown_create_is_not_retried_and_blocks_remaining_rows(self):
        snapshot = _snapshot()
        service, _journal = self._service(snapshot)
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            unknown=True,
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.unknown_count, 1)
        self.assertEqual(
            [item.status for item in run.rows],
            [
                ExecutionRowStatus.OUTCOME_UNKNOWN,
                ExecutionRowStatus.BLOCKED,
                ExecutionRowStatus.BLOCKED,
            ],
        )

    def test_interrupted_create_resumes_only_after_exact_readback(self):
        snapshot = _snapshot()
        service, journal = self._service(snapshot)
        scope_hash = execution_api_scope(snapshot).semantic_hash

        with self.assertRaisesRegex(RuntimeError, "process interruption"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=_CrashAfterCheckpoint(scope_hash),
                actor=LOCAL_ACTOR,
            )

        interrupted = journal.run
        self.assertEqual(interrupted.status, ExecutionRunStatus.RUNNING)
        self.assertEqual(interrupted.rows[0].status, ExecutionRowStatus.IN_FLIGHT)
        self.assertEqual(interrupted.rows[0].transport_batch, 0)
        recovery = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=snapshot.workspace_id,
            execution_run_id=interrupted.run_id,
            snapshot_hash=snapshot.semantic_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=ReconciliationRunStatus.FALLOUT,
            verified_at=datetime.now(timezone.utc),
            verified_by="Local operator",
            unchanged_count=0,
            rows=tuple(
                ReconciliationRow(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    target_model=row.target_model,
                    operation=row.disposition,
                    execution_status=interrupted.rows[index].status.value,
                    status=(
                        ReconciliationRowStatus.NOT_APPLIED
                        if index == 0
                        else ReconciliationRowStatus.NOT_WRITTEN
                    ),
                    message="Recovery test evidence",
                    retry_safe=index == 0,
                )
                for index, row in enumerate(snapshot.rows)
            ),
        )
        classified = service._classify_recovery(
            snapshot,
            interrupted,
            recovery,
        )
        journal.record_recovery(
            snapshot.workspace_id,
            interrupted.run_id,
            classified,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            journal.run.rows[0].status,
            ExecutionRowStatus.RETRY_READY,
        )
        executor = _Executor(scope_hash)

        completed = service.resume(
            snapshot.workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(completed.committed_count, 3)
        self.assertTrue(all(item.recovery_hash for item in completed.rows))
        self.assertEqual(completed.rows[0].attempt, 2)
        self.assertEqual(len(executor.creates), 2)
        self.assertEqual(len(executor.updates), 1)

    def test_known_rejection_stops_independent_later_components(self):
        snapshot = _snapshot()
        service, _journal = self._service(snapshot)
        executor = _RejectFirstCreate(execution_api_scope(snapshot).semantic_hash)

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(len(executor.create_attempts), 1)
        self.assertEqual(executor.updates, [])
        self.assertEqual(run.failed_count, 1)
        self.assertEqual(run.blocked_count, 2)

    def test_partial_cycle_resumes_only_the_exact_completion_fields(self):
        snapshot = _remote_cycle_snapshot()
        service, journal = self._service(snapshot)
        scope_hash = execution_api_scope(snapshot).semantic_hash

        with self.assertRaisesRegex(RuntimeError, "completion interruption"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=_CrashDuringCompletion(scope_hash),
                actor=LOCAL_ACTOR,
            )

        interrupted = journal.run
        self.assertEqual(
            tuple(item.status for item in interrupted.rows),
            (
                ExecutionRowStatus.IN_FLIGHT,
                ExecutionRowStatus.COMMITTED,
            ),
        )
        self.assertEqual(interrupted.rows[0].transport_phase, "COMPLETION")
        recovery = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=snapshot.workspace_id,
            execution_run_id=interrupted.run_id,
            snapshot_hash=snapshot.semantic_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=ReconciliationRunStatus.FALLOUT,
            verified_at=datetime.now(timezone.utc),
            verified_by="Local operator",
            unchanged_count=0,
            rows=tuple(
                ReconciliationRow(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    target_model=row.target_model,
                    operation=row.disposition,
                    execution_status=interrupted.rows[index].status.value,
                    status=(
                        ReconciliationRowStatus.DIFFERENT
                        if index == 0
                        else ReconciliationRowStatus.VERIFIED
                    ),
                    odoo_id=interrupted.rows[index].odoo_id,
                    differing_fields=(("second_id",) if index == 0 else ()),
                    message="Only the deferred relationship still differs",
                )
                for index, row in enumerate(snapshot.rows)
            ),
        )
        executor = _Executor(scope_hash)

        completed = service.resume(
            snapshot.workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(completed.committed_count, 2)
        self.assertEqual(len(executor.creates), 0)
        self.assertEqual(
            tuple(
                tuple(sorted(values)) for _model, _record_id, values in executor.updates
            ),
            (("second_id",),),
        )

    def test_generated_receipt_restart_rereads_without_recreating_source(self):
        snapshot = _generated_variant_snapshot()
        service, journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        scope_hash = execution_api_scope(snapshot).semantic_hash

        with self.assertRaisesRegex(
            RuntimeError,
            "generated-receipt interruption",
        ):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=_CrashDuringProjection(scope_hash),
                actor=LOCAL_ACTOR,
            )

        interrupted = journal.run
        self.assertEqual(
            tuple(item.status for item in interrupted.rows),
            (
                ExecutionRowStatus.PARTIALLY_APPLIED,
                ExecutionRowStatus.PLANNED,
            ),
        )
        recovery = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=snapshot.workspace_id,
            execution_run_id=interrupted.run_id,
            snapshot_hash=snapshot.semantic_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=ReconciliationRunStatus.FALLOUT,
            verified_at=datetime.now(timezone.utc),
            verified_by="Local operator",
            unchanged_count=0,
            rows=tuple(
                ReconciliationRow(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    target_model=row.target_model,
                    operation=row.disposition,
                    execution_status=interrupted.rows[index].status.value,
                    status=(
                        ReconciliationRowStatus.VERIFIED
                        if index == 0
                        else ReconciliationRowStatus.NOT_WRITTEN
                    ),
                    odoo_id=(interrupted.rows[index].odoo_id if index == 0 else None),
                    message="Generated-receipt recovery evidence",
                )
                for index, row in enumerate(snapshot.rows)
            ),
        )
        executor = _Executor(scope_hash)

        completed = service.resume(
            snapshot.workspace_id,
            expected_execution_run_id=interrupted.run_id,
            recovery=recovery,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(len(executor.loads), 1)
        self.assertEqual(executor.loads[0][0], "mrp.bom.line")
        self.assertEqual(len(executor.projection_reads), 1)
        self.assertEqual(
            completed.rows[0].projected_receipts[0].odoo_id,
            110,
        )

    def test_scope_comes_from_reviewed_standard_extension_and_custom_fields(self):
        snapshot = _snapshot()
        custom_category = replace(
            snapshot.rows[0],
            target_model="x_vertical.category",
            fields=(FieldIntent("x_legacy_code", "SET_VALUE", "CAT"),),
        )
        extended_contact = replace(
            snapshot.rows[-1],
            fields=(
                FieldIntent("customer_rank", "SET_VALUE", 1),
                FieldIntent("is_company", "SET_VALUE", True),
                FieldIntent("x_impodo_note", "SET_VALUE", "Reviewed"),
            ),
        )
        scoped = replace(
            snapshot,
            datasets=(
                replace(
                    snapshot.datasets[0],
                    target_model="x_vertical.category",
                    identity_fields=("x_legacy_code",),
                ),
                *snapshot.datasets[1:],
            ),
            rows=(custom_category, snapshot.rows[1], extended_contact),
        )

        scope = execution_api_scope(scoped)

        self.assertEqual(
            scope.write_fields("res.partner"),
            frozenset({"customer_rank", "is_company", "x_impodo_note"}),
        )
        self.assertEqual(
            scope.write_fields("x_vertical.category"),
            frozenset({"x_legacy_code"}),
        )

    def test_many2many_values_are_resolved_into_native_odoo_commands(self):
        snapshot = _snapshot()
        contact = replace(
            snapshot.rows[-1],
            fields=(
                FieldIntent("email", "SET_VALUE", "new@example.test"),
                FieldIntent(
                    "x_tag_ids",
                    "SET_VALUE",
                    (
                        BusinessReference("x.tag", ("BLUE",)),
                        BusinessReference("x.tag", ("FOOD",)),
                    ),
                    kind="relation",
                    relation_operation="replace",
                    related_model="x.tag",
                    related_identity_fields=("code",),
                ),
            ),
        )
        scoped = replace(
            snapshot,
            rows=(*snapshot.rows[:-1], contact),
        )
        service, _journal = self._service(scoped)
        executor = _Executor(execution_api_scope(scoped).semantic_hash)

        service.execute(
            scoped.workspace_id,
            expected_snapshot_hash=scoped.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            executor.updates[-1][2]["x_tag_ids"],
            [[6, 0, [50]]],
        )
        self.assertEqual(
            execution_api_scope(scoped).lookup_fields("x.tag"),
            frozenset({"code"}),
        )

    def test_remote_scalar_create_uses_native_load_with_external_id(self):
        snapshot = _snapshot()
        category = snapshot.rows[0]
        remote_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[0],),
            rows=(category,),
            counts={
                "CREATE": 1,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            remote_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(remote_snapshot).semantic_hash)

        run = service.execute(
            remote_snapshot.workspace_id,
            expected_snapshot_hash=remote_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(executor.creates, [])
        self.assertEqual(
            executor.loads,
            [
                (
                    "product.category",
                    ({"name": "Category"},),
                    (category.proposed_external_id,),
                )
            ],
        )

    def test_remote_many2one_create_uses_the_earlier_external_id(self):
        snapshot = _snapshot()
        relationship_snapshot = replace(
            snapshot,
            datasets=snapshot.datasets[:2],
            rows=snapshot.rows[:2],
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            relationship_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(relationship_snapshot).semantic_hash)

        preview = service.current_preview(relationship_snapshot.workspace_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            relationship_snapshot.workspace_id,
            expected_snapshot_hash=relationship_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.committed_count, 2)
        self.assertEqual(executor.loads[0][0], "product.category")
        self.assertEqual(
            executor.loads[1],
            (
                "product.template",
                (
                    {
                        "categ_id/id": snapshot.rows[0].proposed_external_id,
                        "default_code": "P1",
                        "name": "Product",
                    },
                ),
                (snapshot.rows[1].proposed_external_id,),
            ),
        )
        self.assertFalse(hasattr(executor, "lookup"))

    def test_remote_existing_target_many2one_uses_exact_database_id(self):
        snapshot = _snapshot()
        product = replace(
            snapshot.rows[1],
            fields=(
                *snapshot.rows[1].fields[:-1],
                replace(
                    snapshot.rows[1].fields[-1],
                    value=BusinessReference(
                        "product.category",
                        ("Existing Category",),
                    ),
                ),
            ),
        )
        relationship_snapshot = replace(
            snapshot,
            datasets=snapshot.datasets[:2],
            rows=(snapshot.rows[0], product),
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            relationship_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(relationship_snapshot).semantic_hash)

        preview = service.current_preview(relationship_snapshot.workspace_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            relationship_snapshot.workspace_id,
            expected_snapshot_hash=relationship_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(
            executor.lookup,
            (
                "product.category",
                (("name", "=", "Existing Category"),),
            ),
        )
        self.assertEqual(
            executor.loads[1][1][0]["categ_id/.id"],
            "50",
        )

    def test_configured_create_batch_size_reuses_existing_relation_lookup(
        self,
    ):
        snapshot = _snapshot()
        template = replace(
            snapshot.rows[1],
            fields=(
                *snapshot.rows[1].fields[:-1],
                replace(
                    snapshot.rows[1].fields[-1],
                    value=BusinessReference(
                        "product.category",
                        ("Existing Category",),
                    ),
                ),
            ),
        )
        products = tuple(
            replace(
                template,
                row_id="sha256:" + f"{1000 + index:064x}",
                source_row=1000 + index,
                source_trace_id="sha256:" + f"{2000 + index:064x}",
                source_identity=(f"P{index}",),
                business_identity=(f"P{index}",),
                proposed_external_id=f"impodo_test.products_{index}",
            )
            for index in range(51)
        )
        relationship_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[1],),
            rows=products,
            counts={
                "CREATE": len(products),
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        for batch_rows, expected_sizes in (
            (10, [10, 10, 10, 10, 10, 1]),
            (50, [50, 1]),
        ):
            with self.subTest(batch_rows=batch_rows):
                service, journal = self._service(
                    relationship_snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(relationship_snapshot).semantic_hash
                )

                run = service.execute(
                    relationship_snapshot.workspace_id,
                    expected_snapshot_hash=relationship_snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                    batch_rows=batch_rows,
                )

                self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
                self.assertEqual(run.batch_rows, batch_rows)
                self.assertEqual(
                    [len(rows) for _model, rows, _external_ids in executor.loads],
                    expected_sizes,
                )
                self.assertEqual(
                    executor.lookups,
                    [
                        (
                            "product.category",
                            (("name", "=", "Existing Category"),),
                        )
                    ],
                )

    def test_create_batch_size_is_bounded_before_journaling(self):
        snapshot = _snapshot()
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        for batch_rows in (0, 51, "ten", True):
            with self.subTest(batch_rows=batch_rows):
                service, journal = self._service(snapshot)
                with self.assertRaisesRegex(
                    Exception,
                    "Rows per Odoo batch",
                ):
                    service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                        batch_rows=batch_rows,
                    )
                self.assertIsNone(journal.run)

    def test_remote_load_serializes_every_scalar_as_odoo_import_text(self):
        snapshot = _snapshot()
        category = replace(
            snapshot.rows[0],
            fields=(
                FieldIntent("active", "SET_VALUE", True),
                FieldIntent("archived", "SET_VALUE", False),
                FieldIntent("blank", "SET_NULL"),
                FieldIntent("count", "SET_VALUE", 12),
                FieldIntent("measured", "SET_VALUE", 1.25),
                FieldIntent("name", "SET_VALUE", "Category"),
                FieldIntent("on_date", "SET_VALUE", date(2026, 8, 10)),
                FieldIntent(
                    "on_datetime",
                    "SET_VALUE",
                    datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc),
                ),
                FieldIntent("precise", "SET_VALUE", Decimal("1234.500")),
            ),
        )
        remote_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[0],),
            rows=(category,),
            counts={
                "CREATE": 1,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            remote_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(remote_snapshot).semantic_hash)

        run = service.execute(
            remote_snapshot.workspace_id,
            expected_snapshot_hash=remote_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(
            executor.loads[0][1],
            (
                {
                    "active": "1",
                    "archived": "0",
                    "blank": "",
                    "count": "12",
                    "measured": "1.25",
                    "name": "Category",
                    "on_date": "2026-08-10",
                    "on_datetime": "2026-08-10 14:30:00",
                    "precise": "1234.500",
                },
            ),
        )

    def test_remote_unresolved_target_relation_has_actionable_scope_error(self):
        snapshot = _snapshot()
        product = replace(
            snapshot.rows[1],
            fields=(
                *snapshot.rows[1].fields[:-1],
                replace(
                    snapshot.rows[1].fields[-1],
                    value=LogicalReference(
                        origin="target",
                        key=("Missing Category",),
                        model="product.category",
                        target_fields=("name",),
                    ),
                ),
            ),
        )
        relationship_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[1],),
            rows=(product,),
            counts={
                "CREATE": 1,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            relationship_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )

        preview = service.current_preview(relationship_snapshot.workspace_id)

        assert preview is not None
        self.assertFalse(preview.can_load)
        self.assertEqual(
            preview.scope_error,
            "products.categ_id has no unique reviewed Odoo relationship match",
        )

    def test_remote_existing_target_many2one_requires_one_match(self):
        snapshot = _snapshot()
        product = replace(
            snapshot.rows[1],
            fields=(
                *snapshot.rows[1].fields[:-1],
                replace(
                    snapshot.rows[1].fields[-1],
                    value=BusinessReference(
                        "product.category",
                        ("Existing Category",),
                    ),
                ),
            ),
        )
        relationship_snapshot = replace(
            snapshot,
            datasets=snapshot.datasets[:2],
            rows=(snapshot.rows[0], product),
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        for lookup_ids in ((), (50, 51)):
            with self.subTest(lookup_ids=lookup_ids):
                service, journal = self._service(
                    relationship_snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(relationship_snapshot).semantic_hash,
                    lookup_ids=lookup_ids,
                )

                with self.assertRaisesRegex(
                    WorkspaceError,
                    "no longer matches exactly one record",
                ):
                    service.execute(
                        relationship_snapshot.workspace_id,
                        expected_snapshot_hash=relationship_snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                    )

                self.assertIsNone(journal.run)
                self.assertEqual(executor.loads, [])

    def test_remote_many2many_create_links_imported_and_existing_records(self):
        snapshot = _remote_many2many_snapshot()
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        preview = service.current_preview(snapshot.workspace_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.committed_count, 2)
        self.assertEqual(
            executor.loads[1][1][0]["x_category_ids/.id"],
            "10,50",
        )

    def test_remote_many2many_create_blocks_a_non_unique_existing_member(self):
        snapshot = _remote_many2many_snapshot()
        for lookup_ids in ((), (50, 51)):
            with self.subTest(lookup_ids=lookup_ids):
                service, journal = self._service(
                    snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(snapshot).semantic_hash,
                    lookup_ids=lookup_ids,
                )

                with self.assertRaisesRegex(
                    WorkspaceError,
                    "no longer matches exactly one record",
                ):
                    service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                    )

                self.assertIsNone(journal.run)
                self.assertEqual(executor.loads, [])

    def test_remote_scalar_update_rematches_and_writes_one_exact_record(self):
        snapshot = _snapshot()
        contact = snapshot.rows[-1]
        update_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=(contact,),
            counts={
                "CREATE": 0,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(update_snapshot).semantic_hash)

        preview = service.current_preview(update_snapshot.workspace_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            update_snapshot.workspace_id,
            expected_snapshot_hash=update_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(executor.lookup, ("res.partner", (("ref", "=", "C1"),)))
        self.assertEqual(
            executor.updates,
            [("res.partner", 50, {"email": "new@example.test"})],
        )
        self.assertEqual(run.rows[0].status, ExecutionRowStatus.COMMITTED)
        self.assertEqual(run.rows[0].odoo_id, 50)

    def test_remote_update_requires_one_current_business_key_match(self):
        snapshot = _snapshot()
        update_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=(snapshot.rows[-1],),
            counts={
                "CREATE": 0,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(update_snapshot).semantic_hash,
            lookup_ids=(),
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "no longer matches exactly one record",
        ):
            service.execute(
                update_snapshot.workspace_id,
                expected_snapshot_hash=update_snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
            )

        self.assertIsNone(journal.run)
        self.assertEqual(executor.updates, [])

    def test_remote_update_unknown_outcome_is_not_retried(self):
        snapshot = _snapshot()
        update_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=(snapshot.rows[-1],),
            counts={
                "CREATE": 0,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(update_snapshot).semantic_hash,
            update_error=OdooWriteOutcomeUnknown("lost response"),
        )

        run = service.execute(
            update_snapshot.workspace_id,
            expected_snapshot_hash=update_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.rows[0].status, ExecutionRowStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.rows[0].attempt, 1)
        self.assertEqual(executor.updates, [])

    def test_remote_update_rejection_is_recorded_as_failed(self):
        snapshot = _snapshot()
        update_snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=(snapshot.rows[-1],),
            counts={
                "CREATE": 0,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(update_snapshot).semantic_hash,
            update_error=OdooWriteRejected("Odoo rejected the load request"),
        )

        run = service.execute(
            update_snapshot.workspace_id,
            expected_snapshot_hash=update_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(run.rows[0].status, ExecutionRowStatus.FAILED)
        self.assertEqual(run.rows[0].attempt, 1)
        self.assertEqual(executor.updates, [])

    def test_remote_many2one_update_writes_one_resolved_record_id(self):
        snapshot = _remote_relation_update_snapshot(many2many=False)
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            lookup_ids=(),
            lookup_results={
                ("res.partner", (("ref", "=", "C1"),)): (50,),
                ("res.partner", (("ref", "=", "PARENT"),)): (60,),
            },
        )

        preview = service.current_preview(snapshot.workspace_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(
            executor.updates,
            [("res.partner", 50, {"parent_id": 60})],
        )

    def test_remote_many2many_update_writes_one_exact_final_set(self):
        snapshot = _remote_relation_update_snapshot(many2many=True)
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            lookup_ids=(),
            lookup_results={
                ("res.partner", (("ref", "=", "C1"),)): (50,),
                ("x.tag", (("code", "=", "BLUE"),)): (60,),
                ("x.tag", (("code", "=", "FOOD"),)): (61,),
            },
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(
            executor.updates,
            [("res.partner", 50, {"x_tag_ids": [[6, 0, [60, 61]]]})],
        )

    def test_remote_relationship_update_uses_an_earlier_import_receipt(self):
        snapshot = _snapshot()
        contact = replace(
            snapshot.rows[-1],
            fields=(
                FieldIntent(
                    "x_category_id",
                    "SET_VALUE",
                    LogicalReference(
                        origin="incoming",
                        key=("CAT",),
                        dataset="categories",
                    ),
                    kind="relation",
                    relation_operation="replace",
                    related_model="product.category",
                    related_identity_fields=("name",),
                ),
            ),
        )
        update_snapshot = replace(
            snapshot,
            datasets=(
                snapshot.datasets[0],
                replace(snapshot.datasets[-1], dependencies=("categories",)),
            ),
            rows=(snapshot.rows[0], contact),
            counts={
                "CREATE": 1,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(update_snapshot).semantic_hash,
            lookup_ids=(),
            lookup_results={
                ("res.partner", (("ref", "=", "C1"),)): (50,),
            },
        )

        run = service.execute(
            update_snapshot.workspace_id,
            expected_snapshot_hash=update_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(
            executor.updates,
            [("res.partner", 50, {"x_category_id": 10})],
        )

    def test_remote_relationship_update_blocks_a_non_unique_member(self):
        snapshot = _remote_relation_update_snapshot(many2many=True)
        for blue_ids in ((), (60, 62)):
            with self.subTest(blue_ids=blue_ids):
                service, journal = self._service(
                    snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(snapshot).semantic_hash,
                    lookup_ids=(),
                    lookup_results={
                        ("res.partner", (("ref", "=", "C1"),)): (50,),
                        ("x.tag", (("code", "=", "BLUE"),)): blue_ids,
                        ("x.tag", (("code", "=", "FOOD"),)): (61,),
                    },
                )

                with self.assertRaisesRegex(
                    WorkspaceError,
                    "no longer matches exactly one record",
                ):
                    service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                    )

                self.assertIsNone(journal.run)
                self.assertEqual(executor.updates, [])

    def test_remote_relationship_update_rejects_incremental_commands(self):
        snapshot = _remote_relation_update_snapshot(many2many=True)
        contact = replace(
            snapshot.rows[0],
            fields=(replace(snapshot.rows[0].fields[0], relation_operation="add"),),
        )
        snapshot = replace(snapshot, rows=(contact,))
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )

        preview = service.current_preview(snapshot.workspace_id)

        assert preview is not None
        self.assertFalse(preview.can_load)
        self.assertIn("exact relationship replacement", preview.scope_error)

    def test_remote_create_cycle_uses_create_then_relationship_patch(self):
        snapshot = _remote_cycle_snapshot()
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        preview = service.current_preview(snapshot.workspace_id)

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview.deferred_create_count, 1)
        self.assertEqual(preview.dependency_summary.relationship_record_count, 1)
        self.assertEqual(preview.dependency_summary.relationship_field_count, 1)
        self.assertGreaterEqual(preview.dependency_summary.total_group_count, 1)
        self.assertTrue(preview.dependency_summary.groups)

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.committed_count, 2)
        self.assertEqual(run.partially_applied_count, 0)
        self.assertEqual(
            executor.loads,
            [
                (
                    "x.first.node",
                    ({"code": "FIRST"},),
                    ("impodo_test.first_nodes_20",),
                ),
                (
                    "x.second.node",
                    (
                        {
                            "code": "SECOND",
                            "first_id/id": "impodo_test.first_nodes_20",
                        },
                    ),
                    ("impodo_test.second_nodes_21",),
                ),
            ],
        )
        self.assertEqual(
            executor.updates,
            [("x.first.node", 10, {"second_id": 11})],
        )

    def test_deferred_relationship_progress_advances_after_each_odoo_call(self):
        snapshot = _remote_cycle_snapshot()
        first = snapshot.rows[0]
        another_first = replace(
            first,
            row_id="sha256:" + f"{22:064x}",
            source_row=22,
            source_trace_id="sha256:" + f"{122:064x}",
            source_identity=("FIRST-2",),
            business_identity=("FIRST-2",),
            proposed_external_id="impodo_test.first_nodes_22",
            fields=(replace(first.fields[0], value="FIRST-2"), first.fields[1]),
        )
        snapshot = replace(
            snapshot,
            rows=(first, another_first, snapshot.rows[1]),
            counts={
                "CREATE": 3,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        progress = []

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=_Executor(execution_api_scope(snapshot).semantic_hash),
            actor=LOCAL_ACTOR,
            progress=progress.append,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertIn(1, [item.partially_applied_count for item in progress])

    def test_required_at_create_cycle_is_blocked_before_write(self):
        snapshot = _remote_cycle_snapshot(required_at_create=True)
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )

        preview = service.current_preview(snapshot.workspace_id)

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview.deferred_create_count, 0)
        self.assertFalse(preview.can_load)
        self.assertIn("Required create-time relationships", preview.scope_error)
        self.assertEqual(
            preview.blocker_summary.groups[0].code,
            "HARD_DEPENDENCY_CYCLE",
        )
        self.assertIn(
            "Required relationships",
            preview.blocker_summary.groups[0].title,
        )

    def test_missing_incoming_row_is_blocked_before_journal_or_write(self):
        snapshot = _snapshot()
        product = snapshot.rows[1]
        relation = product.fields[-1]
        product = replace(
            product,
            fields=(
                *product.fields[:-1],
                replace(
                    relation,
                    value=LogicalReference(
                        origin="incoming",
                        key=("MISSING",),
                        dataset="categories",
                    ),
                ),
            ),
        )
        snapshot = replace(
            snapshot,
            rows=(snapshot.rows[0], product, snapshot.rows[2]),
        )
        service, journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        preview = service.current_preview(snapshot.workspace_id)

        assert preview is not None
        self.assertFalse(preview.can_load)
        self.assertIn("cannot be scheduled safely", preview.scope_error)
        self.assertEqual(
            preview.blocker_summary.groups[0].code,
            "MISSING_INCOMING_ROW",
        )
        self.assertEqual(preview.blocker_summary.groups[0].record_count, 1)
        with self.assertRaisesRegex(WorkspaceError, "cannot be scheduled safely"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
            )
        self.assertIsNone(journal.run)
        self.assertEqual(executor.loads, [])
        self.assertEqual(executor.creates, [])

    def test_rejected_cycle_patch_retains_created_id_as_partial(self):
        snapshot = _remote_cycle_snapshot()
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            update_error=OdooWriteRejected("constraint rejected relationship"),
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(run.partially_applied_count, 1)
        self.assertEqual(run.rows[0].odoo_id, 10)
        self.assertIn("Record was created", run.rows[0].safe_error)
        self.assertEqual(run.rows[1].status, ExecutionRowStatus.COMMITTED)

    def test_uncertain_cycle_patch_keeps_exact_created_id(self):
        snapshot = _remote_cycle_snapshot()
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            update_error=OdooWriteOutcomeUnknown("lost patch response"),
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.rows[0].status, ExecutionRowStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.rows[0].odoo_id, 10)
        self.assertEqual(run.rows[1].status, ExecutionRowStatus.COMMITTED)

    def test_existing_identity_retarget_is_rejected_before_journal(self):
        snapshot = _snapshot()
        contact = replace(
            snapshot.rows[-1],
            target_binding_hash=target_record_binding_hash("res.partner", 50),
        )
        snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=(contact,),
            counts={
                "CREATE": 0,
                "UPDATE": 1,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            lookup_ids=(51,),
        )

        with self.assertRaisesRegex(WorkspaceError, "different record"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
            )

        self.assertIsNone(journal.run)
        self.assertEqual(executor.updates, [])
        self.assertEqual(executor.single_lookup_count, 0)

    def test_existing_identities_are_bulk_resolved_in_bounded_pages(self):
        snapshot = _snapshot()
        template = snapshot.rows[-1]
        rows = tuple(
            replace(
                template,
                row_id="sha256:" + f"{index + 1000:064x}",
                source_row=index + 2,
                source_trace_id="sha256:" + f"{index + 2000:064x}",
                source_identity=(f"C{index:03d}",),
                business_identity=(f"C{index:03d}",),
                target_binding_hash="",
            )
            for index in range(101)
        )
        snapshot = replace(
            snapshot,
            datasets=(snapshot.datasets[-1],),
            rows=rows,
            counts={
                "CREATE": 0,
                "UPDATE": len(rows),
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, _journal = self._service(snapshot)
        lookup_results = {
            ("res.partner", (("ref", "=", f"C{index:03d}"),)): (index + 1,)
            for index in range(101)
        }
        executor = _Executor(
            execution_api_scope(snapshot).semantic_hash,
            lookup_results=lookup_results,
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.committed_count, 101)
        self.assertEqual(
            tuple(len(page) for _model, page in executor.bulk_lookups), (100, 1)
        )
        self.assertEqual(executor.single_lookup_count, 0)

    def test_dependency_receipt_is_journalled_before_dependent_write(self):
        snapshot = _snapshot()
        snapshot = replace(
            snapshot,
            datasets=snapshot.datasets[:2],
            rows=snapshot.rows[:2],
            counts={
                "CREATE": 2,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "AMBIGUOUS": 0,
                "BLOCKED": 0,
            },
        )
        service, journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        events = []
        record_outcomes = journal.record_outcomes
        create_rows = executor.create_rows

        def recording_outcomes(project_id, run_id, rows):
            events.append(("journal", tuple(item.dataset for item in rows)))
            record_outcomes(project_id, run_id, rows)

        def recording_create(model, values):
            events.append(("write", model))
            return create_rows(model, values)

        journal.record_outcomes = recording_outcomes
        executor.create_rows = recording_create

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertLess(
            events.index(("journal", ("categories",))),
            events.index(("write", "product.template")),
        )


class Json2WriteExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []

        def transport(url, headers, body, timeout, method):
            self.calls.append((url, headers, body, timeout, method))
            if url.endswith("/search_read"):
                return 200, [{"id": 42}]
            if url.endswith("/load"):
                return 200, {"ids": [43], "messages": [], "nextrow": 0}
            if url.endswith("/create"):
                return 200, [43]
            return 200, True

        self.scope = OdooApiScope(
            preview_hash=HASH,
            models=(
                OdooModelScope(
                    "res.partner",
                    write_fields=(
                        "customer_rank",
                        "email",
                        "name",
                        "parent_id",
                        "x_impodo_note",
                        "x_tag_ids",
                    ),
                    read_fields=(
                        "customer_rank",
                        "email",
                        "name",
                        "parent_id",
                        "x_impodo_note",
                        "x_tag_ids",
                    ),
                    lookup_fields=("ref",),
                ),
                OdooModelScope(
                    "x_impodo.asset",
                    write_fields=("name", "x_legacy_code"),
                    read_fields=("name", "x_legacy_code"),
                    lookup_fields=("x_legacy_code",),
                ),
            ),
        )
        self.executor = Json2WriteExecutor(
            Json2Config(
                base_url="http://127.0.0.1:8069",
                database="odoo19_disposable",
                api_key="secret",
                connection_mode="LOCAL",
            ),
            self.scope,
            transport=transport,
        )

    def test_closed_methods_and_fields_support_exact_native_operations(self):
        self.assertEqual(
            self.executor.find_ids("res.partner", (("ref", "=", "C1"),)),
            (42,),
        )
        self.assertEqual(
            self.executor.create_rows("res.partner", ({"name": "Contact"},)),
            (43,),
        )
        self.executor.update_row("res.partner", 42, {"email": "a@example.test"})
        self.assertEqual(
            self.executor.create_rows(
                "x_impodo.asset",
                ({"name": "Asset", "x_legacy_code": "A-1"},),
            ),
            (43,),
        )
        self.executor.update_row("res.partner", 42, {"customer_rank": 1})
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(self.calls[1][0].endswith("/json/2/res.partner/create"))
        self.assertNotIn(b"secret", self.calls[1][2])

    def test_bounded_exact_ids_share_one_update_call(self):
        self.executor.update_rows(
            "res.partner",
            (41, 42, 43),
            {"customer_rank": 1},
        )

        self.assertEqual(len(self.calls), 1)
        payload = json.loads(self.calls[0][2])
        self.assertEqual(payload["ids"], [41, 42, 43])
        self.assertEqual(payload["vals"], {"customer_rank": 1})

    def test_bulk_lookup_uses_one_bounded_or_query_and_returns_positional_matches(self):
        calls = []

        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            calls.append((url, json.loads(body)))
            return 200, [
                {"id": 41, "ref": "C1"},
                {"id": 42, "ref": "C2"},
            ]

        executor = Json2WriteExecutor(
            self.executor.config,
            self.scope,
            transport=transport,
        )

        matches = executor.find_ids_many(
            "res.partner",
            (
                (("ref", "=", "C1"),),
                (("ref", "=", "C2"),),
            ),
        )

        self.assertEqual(matches, ((41,), (42,)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1]["domain"],
            ["|", ["ref", "=", "C1"], ["ref", "=", "C2"]],
        )
        self.assertEqual(calls[0][1]["limit"], 5)

    def test_generated_receipt_readback_is_exact_bounded_and_positional(self):
        calls = []

        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            calls.append((url, json.loads(body)))
            return 200, [
                {"id": 42, "product_variant_id": [142, "Variant B"]},
                {"id": 41, "product_variant_id": [141, "Variant A"]},
            ]

        scope = OdooApiScope(
            preview_hash=HASH,
            models=(
                OdooModelScope(
                    "product.product",
                    read_fields=("default_code",),
                    lookup_fields=("default_code",),
                ),
                OdooModelScope(
                    "product.template",
                    read_fields=("product_variant_id",),
                    lookup_fields=("default_code",),
                ),
            ),
        )
        executor = Json2WriteExecutor(
            self.executor.config,
            scope,
            transport=transport,
        )

        projected = executor.read_projected_ids(
            "product.template",
            (41, 42),
            "product_variant_id",
            "product.product",
        )

        self.assertEqual(projected, (141, 142))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1],
            {
                "context": {},
                "domain": [["id", "in", [41, 42]]],
                "fields": ["id", "product_variant_id"],
                "limit": 2,
                "order": "id asc",
            },
        )
        with self.assertRaises(OdooWriteRejected):
            executor.read_projected_ids(
                "product.template",
                (41,),
                "unreviewed_field",
                "product.product",
            )

    def test_rejects_fields_and_models_not_present_in_the_reviewed_preview(self):
        with self.assertRaises(OdooWriteRejected):
            self.executor.create_rows("account.move", ({"name": "No"},))
        with self.assertRaises(OdooWriteRejected):
            self.executor.create_rows("res.partner", ({"password": "No"},))
        with self.assertRaises(OdooWriteRejected):
            self.executor.find_ids("res.partner", ())
        with self.assertRaises(OdooWriteRejected):
            self.executor.create_rows(
                "res.partner",
                tuple({"name": f"Contact {index}"} for index in range(51)),
            )

    def test_native_update_sends_one_exact_id_and_reviewed_scalar_values(self):
        self.executor.update_row(
            "res.partner",
            42,
            {"email": "a@example.test", "x_impodo_note": "Reviewed"},
        )

        url, headers, body, _timeout, method = self.calls[-1]
        self.assertTrue(url.endswith("/json/2/res.partner/write"))
        self.assertEqual(method, "POST")
        self.assertNotIn(b"secret", body)
        self.assertEqual(
            json.loads(body),
            {
                "context": {},
                "ids": [42],
                "vals": {
                    "email": "a@example.test",
                    "x_impodo_note": "Reviewed",
                },
            },
        )

    def test_native_update_sends_reviewed_relationship_values(self):
        self.executor.update_row(
            "res.partner",
            42,
            {
                "parent_id": 60,
                "x_tag_ids": [[6, 0, [60, 61]]],
            },
        )

        self.assertEqual(
            json.loads(self.calls[-1][2]),
            {
                "context": {},
                "ids": [42],
                "vals": {
                    "parent_id": 60,
                    "x_tag_ids": [[6, 0, [60, 61]]],
                },
            },
        )

    def test_native_load_sends_external_ids_and_reviewed_scalar_fields(self):
        identifiers = self.executor.load_create_rows(
            "res.partner",
            (
                {
                    "name": "Contact",
                    "parent_id/id": "impodo_test.parent_1",
                    "x_impodo_note": "Reviewed",
                },
            ),
            ("impodo_test.contact_1",),
        )

        self.assertEqual(identifiers, (43,))
        url, headers, body, _timeout, method = self.calls[-1]
        self.assertTrue(url.endswith("/json/2/res.partner/load"))
        self.assertEqual(method, "POST")
        self.assertEqual(headers["X-Odoo-Database"], "odoo19_disposable")
        self.assertNotIn(b"secret", body)
        self.assertEqual(
            json.loads(body),
            {
                "context": {"import_file": True},
                "data": [
                    [
                        "impodo_test.contact_1",
                        "Contact",
                        "impodo_test.parent_1",
                        "Reviewed",
                    ],
                ],
                "fields": ["id", "name", "parent_id/id", "x_impodo_note"],
            },
        )

    def test_native_load_rejects_unreviewed_relationship_paths(self):
        for values in (
            {"password/id": "impodo_test.secret_1"},
            {"parent_id/name": "Parent"},
            {"parent_id/id/name": "Parent"},
        ):
            with self.subTest(values=values), self.assertRaises(OdooWriteRejected):
                self.executor.load_create_rows(
                    "res.partner",
                    (values,),
                    ("impodo_test.contact_1",),
                )

    def test_native_load_rejects_non_text_import_cells(self):
        for value in (True, False, None, 1, 1.25):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    OdooWriteRejected,
                    "text format",
                ),
            ):
                self.executor.load_create_rows(
                    "res.partner",
                    ({"customer_rank": value, "name": "Contact"},),
                    ("impodo_test.contact_1",),
                )

    def test_native_load_accepts_one_reviewed_database_id_relationship(self):
        identifiers = self.executor.load_create_rows(
            "res.partner",
            ({"name": "Contact", "parent_id/.id": "42"},),
            ("impodo_test.contact_1",),
        )

        self.assertEqual(identifiers, (43,))
        self.assertEqual(
            json.loads(self.calls[-1][2]),
            {
                "context": {"import_file": True},
                "data": [["impodo_test.contact_1", "Contact", "42"]],
                "fields": ["id", "name", "parent_id/.id"],
            },
        )

    def test_native_load_accepts_reviewed_many2many_database_ids(self):
        identifiers = self.executor.load_create_rows(
            "res.partner",
            ({"name": "Contact", "x_tag_ids/.id": "10,50"},),
            ("impodo_test.contact_1",),
        )

        self.assertEqual(identifiers, (43,))
        self.assertEqual(
            json.loads(self.calls[-1][2]),
            {
                "context": {"import_file": True},
                "data": [["impodo_test.contact_1", "Contact", "10,50"]],
                "fields": ["id", "name", "x_tag_ids/.id"],
            },
        )

    def test_native_load_distinguishes_rejection_from_uncertain_receipt(self):
        rejected = Json2WriteExecutor(
            self.executor.config,
            self.scope,
            transport=lambda *_args: (
                200,
                {
                    "ids": False,
                    "messages": [
                        {
                            "type": "error",
                            "message": ("A required field contains production-secret"),
                        }
                    ],
                    "nextrow": 0,
                },
            ),
        )
        with self.assertRaisesRegex(
            OdooWriteRejected,
            "rejected one or more imported rows",
        ) as caught:
            rejected.load_create_rows(
                "res.partner",
                ({"name": "Contact"},),
                ("impodo_test.contact_1",),
            )
        self.assertNotIn("production-secret", str(caught.exception))

        invalid = Json2WriteExecutor(
            self.executor.config,
            self.scope,
            transport=lambda *_args: (
                200,
                {"ids": [43], "messages": "invalid", "nextrow": 0},
            ),
        )
        with self.assertRaises(OdooWriteOutcomeUnknown):
            invalid.load_create_rows(
                "res.partner",
                ({"name": "Contact"},),
                ("impodo_test.contact_1",),
            )

    def test_server_error_and_invalid_create_receipt_are_uncertain(self):
        for status in (408, 429, 500):
            with self.subTest(status=status):
                server_error = Json2WriteExecutor(
                    self.executor.config,
                    self.scope,
                    transport=lambda *_args, status=status: (status, None),
                )
                with self.assertRaises(OdooWriteOutcomeUnknown):
                    server_error.create_rows(
                        "res.partner",
                        ({"name": "Contact"},),
                    )

        invalid_receipt = Json2WriteExecutor(
            self.executor.config,
            self.scope,
            transport=lambda *_args: (200, [True]),
        )
        with self.assertRaises(OdooWriteOutcomeUnknown):
            invalid_receipt.create_rows(
                "res.partner",
                ({"name": "Contact"},),
            )


class TargetWriterFactoryTests(unittest.TestCase):
    def test_remote_https_target_builds_the_closed_json2_writer(self):
        scope = OdooApiScope(
            preview_hash=HASH,
            models=(
                OdooModelScope(
                    "res.partner",
                    write_fields=("name",),
                    read_fields=("name",),
                ),
            ),
        )
        project = SimpleNamespace(
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="fresh_migration",
        )

        executor = _write_executor(project, "secret", scope)

        self.assertIsInstance(executor, Json2WriteExecutor)
        self.assertEqual(executor.config.connection_mode, "REMOTE")
        self.assertEqual(executor.config.retries, 0)


if __name__ == "__main__":
    unittest.main()
