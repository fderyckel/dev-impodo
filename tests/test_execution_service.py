from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.execution_service import ExecutionService, execution_api_scope
from impodo.domain.execution import (
    ExecutionRowStatus,
    ExecutionRunStatus,
)
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
)
from impodo.models import BusinessReference, LogicalReference, OdooWriteIdentity
from impodo.odoo_writer import (
    Json2WriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.odoo_scope import OdooApiScope, OdooModelScope
from impodo.connectors import Json2Config
from impodo.projects import OdooConnectionMode, SourceMode
from impodo.web.target_writers import _write_executor
from impodo.workspace_errors import WorkspaceError


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
    return ExecutionSnapshot(
        project_id=str(uuid4()),
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
    return replace(
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
    return replace(
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
                defer_on_create=True,
            ),
        ),
    )
    return replace(
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


class _Journal:
    def __init__(self) -> None:
        self.run = None
        self.rows = {}

    def get_current_run(self, project_id, snapshot_hash=None):
        del project_id, snapshot_hash
        return self.run if self.run and self.run.status is not ExecutionRunStatus.RUNNING else None

    def start_run(self, project_id, run, *, actor):
        del project_id, actor
        self.run = run
        self.rows = {item.row_id: item for item in run.rows}

    def record_outcomes(self, project_id, run_id, rows):
        del project_id, run_id
        self.rows.update({item.row_id: item for item in rows})

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
        self.creates = []
        self.loads = []
        self.updates = []
        self.next_id = 10

    def find_ids(self, model, domain):
        self.lookup = (model, tuple(domain))
        self.lookups.append(self.lookup)
        return self.lookup_results.get(self.lookup, self.lookup_ids)

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


class ExecutionServiceTests(unittest.TestCase):
    def _service(self, snapshot, *, mode=OdooConnectionMode.LOCAL):
        journal = _Journal()
        project = SimpleNamespace(
            project_id=snapshot.project_id,
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

    def test_changed_confirmation_hash_stops_before_journal_or_target_io(self):
        snapshot = _snapshot()
        service, journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        with self.assertRaisesRegex(WorkspaceError, "preview changed"):
            service.execute(
                snapshot.project_id,
                expected_snapshot_hash="sha256:" + "9" * 64,
                executor=executor,
                actor=LOCAL_ACTOR,
            )

        self.assertIsNone(journal.run)
        self.assertEqual(executor.creates, [])
        self.assertEqual(executor.updates, [])

    def test_loaded_preview_cannot_be_submitted_again(self):
        snapshot = _snapshot()
        service, journal = self._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        service.execute(
            snapshot.project_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )
        first_creates = tuple(executor.creates)
        first_updates = tuple(executor.updates)

        with self.assertRaisesRegex(WorkspaceError, "already loaded"):
            service.execute(
                snapshot.project_id,
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

        run = service.execute(
            snapshot.project_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.committed_count, 3)
        self.assertEqual(executor.creates[0][0], "product.category")
        self.assertEqual(executor.creates[1][1][0]["categ_id"], 10)
        self.assertEqual(
            executor.updates,
            [("res.partner", 50, {"email": "new@example.test"})],
        )

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
            snapshot.project_id,
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
                snapshot.project_id,
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
                snapshot.project_id,
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
            snapshot.project_id,
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
            scoped.project_id,
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
            remote_snapshot.project_id,
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

        preview = service.current_preview(relationship_snapshot.project_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            relationship_snapshot.project_id,
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

        preview = service.current_preview(relationship_snapshot.project_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            relationship_snapshot.project_id,
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
                service, _journal = self._service(
                    relationship_snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(relationship_snapshot).semantic_hash
                )

                run = service.execute(
                    relationship_snapshot.project_id,
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
                        snapshot.project_id,
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
            remote_snapshot.project_id,
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

        preview = service.current_preview(relationship_snapshot.project_id)

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
                service, _journal = self._service(
                    relationship_snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(relationship_snapshot).semantic_hash,
                    lookup_ids=lookup_ids,
                )

                run = service.execute(
                    relationship_snapshot.project_id,
                    expected_snapshot_hash=relationship_snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                )

                self.assertEqual(
                    run.status,
                    ExecutionRunStatus.COMPLETED_WITH_ERRORS,
                )
                self.assertEqual(
                    run.rows[0].status,
                    ExecutionRowStatus.COMMITTED,
                )
                self.assertEqual(
                    run.rows[1].status,
                    ExecutionRowStatus.BLOCKED,
                )
                self.assertEqual(len(executor.loads), 1)

    def test_remote_many2many_create_links_imported_and_existing_records(self):
        snapshot = _remote_many2many_snapshot()
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)

        preview = service.current_preview(snapshot.project_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            snapshot.project_id,
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
                service, _journal = self._service(
                    snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                executor = _Executor(
                    execution_api_scope(snapshot).semantic_hash,
                    lookup_ids=lookup_ids,
                )

                run = service.execute(
                    snapshot.project_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                )

                self.assertEqual(
                    run.status,
                    ExecutionRunStatus.COMPLETED_WITH_ERRORS,
                )
                self.assertEqual(
                    tuple(item.status for item in run.rows),
                    (
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.BLOCKED,
                    ),
                )
                self.assertEqual(len(executor.loads), 1)

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
        service, _journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(execution_api_scope(update_snapshot).semantic_hash)

        preview = service.current_preview(update_snapshot.project_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            update_snapshot.project_id,
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
        service, _journal = self._service(
            update_snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        executor = _Executor(
            execution_api_scope(update_snapshot).semantic_hash,
            lookup_ids=(),
        )

        run = service.execute(
            update_snapshot.project_id,
            expected_snapshot_hash=update_snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(run.rows[0].status, ExecutionRowStatus.BLOCKED)
        self.assertEqual(run.rows[0].attempt, 0)
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
            update_snapshot.project_id,
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
            update_snapshot.project_id,
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

        preview = service.current_preview(snapshot.project_id)

        assert preview is not None
        self.assertTrue(preview.can_load, preview.scope_error)

        run = service.execute(
            snapshot.project_id,
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
            snapshot.project_id,
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
            update_snapshot.project_id,
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
                service, _journal = self._service(
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

                run = service.execute(
                    snapshot.project_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                )

                self.assertEqual(
                    run.status,
                    ExecutionRunStatus.COMPLETED_WITH_ERRORS,
                )
                self.assertEqual(
                    run.rows[0].status,
                    ExecutionRowStatus.BLOCKED,
                )
                self.assertEqual(run.rows[0].attempt, 0)
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

        preview = service.current_preview(snapshot.project_id)

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
        preview = service.current_preview(snapshot.project_id)

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview.deferred_create_count, 1)

        run = service.execute(
            snapshot.project_id,
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

    def test_required_at_create_cycle_is_blocked_before_write(self):
        snapshot = _remote_cycle_snapshot(required_at_create=True)
        service, _journal = self._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )

        preview = service.current_preview(snapshot.project_id)

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview.deferred_create_count, 0)
        self.assertFalse(preview.can_load)
        self.assertIn("required during create", preview.scope_error)

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
            snapshot.project_id,
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
            snapshot.project_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(run.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.rows[0].status, ExecutionRowStatus.OUTCOME_UNKNOWN)
        self.assertEqual(run.rows[0].odoo_id, 10)
        self.assertEqual(run.rows[1].status, ExecutionRowStatus.COMMITTED)


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
            )
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
            with self.subTest(value=value), self.assertRaisesRegex(
                OdooWriteRejected,
                "text format",
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
                        {"type": "error", "message": "A required field is missing"}
                    ],
                    "nextrow": 0,
                },
            ),
        )
        with self.assertRaisesRegex(OdooWriteRejected, "required field"):
            rejected.load_create_rows(
                "res.partner",
                ({"name": "Contact"},),
                ("impodo_test.contact_1",),
            )

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
