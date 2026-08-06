from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.execution_service import ExecutionService
from impodo.domain.execution import (
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
)
from impodo.models import LogicalReference
from impodo.odoo_writer import (
    Json2WriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.connectors import Json2Config
from impodo.projects import OdooConnectionMode


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

    def __init__(self, *, unknown=False) -> None:
        self.unknown = unknown
        self.creates = []
        self.updates = []
        self.next_id = 10

    def find_ids(self, model, domain):
        self.lookup = (model, tuple(domain))
        return (50,)

    def create_rows(self, model, values):
        if self.unknown:
            raise OdooWriteOutcomeUnknown("lost response")
        rows = tuple(dict(item) for item in values)
        self.creates.append((model, rows))
        ids = tuple(range(self.next_id, self.next_id + len(rows)))
        self.next_id += len(rows)
        return ids

    def update_row(self, model, record_id, values):
        self.updates.append((model, record_id, dict(values)))


class ExecutionServiceTests(unittest.TestCase):
    def _service(self, snapshot):
        journal = _Journal()
        project = SimpleNamespace(
            project_id=snapshot.project_id,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
        )
        service = ExecutionService(
            SimpleNamespace(get=lambda _project_id: project),
            SimpleNamespace(current_execution_snapshot=lambda _project_id: snapshot),
            journal,
            CapabilityAuthorizationPolicy(),
        )
        return service, journal

    def test_loads_dependency_order_batches_creates_and_updates_unique_match(self):
        snapshot = _snapshot()
        service, _journal = self._service(snapshot)
        executor = _Executor()

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

    def test_unknown_create_is_not_retried_and_blocks_remaining_rows(self):
        snapshot = _snapshot()
        service, _journal = self._service(snapshot)
        executor = _Executor(unknown=True)

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


class Json2WriteExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []

        def transport(url, headers, body, timeout, method):
            self.calls.append((url, headers, body, timeout, method))
            if url.endswith("/search_read"):
                return 200, [{"id": 42}]
            if url.endswith("/create"):
                return 200, [43]
            return 200, True

        self.executor = Json2WriteExecutor(
            Json2Config(
                base_url="http://127.0.0.1:8069",
                database="odoo19_disposable",
                api_key="secret",
                connection_mode="LOCAL",
            ),
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
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.calls[1][0].endswith("/json/2/res.partner/create"))
        self.assertNotIn(b"secret", self.calls[1][2])

    def test_rejects_unapproved_model_field_and_unrestricted_lookup(self):
        with self.assertRaises(OdooWriteRejected):
            self.executor.create_rows("account.move", ({"name": "No"},))
        with self.assertRaises(OdooWriteRejected):
            self.executor.create_rows("res.partner", ({"password": "No"},))
        with self.assertRaises(OdooWriteRejected):
            self.executor.find_ids("res.partner", ())


if __name__ == "__main__":
    unittest.main()
