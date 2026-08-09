from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.execution_service import execution_api_scope
from impodo.application.reconciliation_service import ReconciliationService
from impodo.connectors import Json2Config
from impodo.domain.execution import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.reconciliation import (
    ReconciliationRowStatus,
    ReconciliationRunStatus,
)
from impodo.domain.execution_snapshot import FieldIntent
from impodo.models import BusinessReference
from impodo.odoo_readback import (
    ExternalIdBinding,
    Json2ReadbackReader,
    OdooReadbackError,
    ReadbackRecord,
)
from impodo.odoo_scope import OdooApiScope, OdooModelScope

from tests.test_execution_service import (
    HASH,
    TARGET_HASH,
    _remote_many2many_snapshot,
    _snapshot,
)


class _Execution:
    def __init__(self, run):
        self.run = run

    def get_run(self, project_id, run_id):
        return self.run if (project_id, run_id) == (self.run.project_id, self.run.run_id) else None

    def get_current_run(self, project_id, snapshot_hash=None):
        del snapshot_hash
        return self.run if project_id == self.run.project_id else None


class _Results:
    def __init__(self):
        self.report = None

    def get_current(self, project_id, execution_run_id=None):
        if self.report is None or self.report.project_id != project_id:
            return None
        if execution_run_id and self.report.execution_run_id != execution_run_id:
            return None
        return self.report

    def publish(self, project_id, report, *, actor):
        del actor
        assert report.project_id == project_id
        self.report = report


class _Reader:
    target_hash = TARGET_HASH
    imports_external_ids = True

    def __init__(self, scope_hash):
        self.scope_hash = scope_hash
        self.records = {
            ("product.category", 10): {"name": "Category"},
            ("product.template", 11): {
                "default_code": "P1",
                "name": "Product",
                "categ_id": [10, "Category"],
            },
            ("res.partner", 50): {"email": "new@example.test"},
        }
        self.uncertain = ()
        self.references = {}
        self.external_ids = {
            "impodo_test.categories_2": ExternalIdBinding(
                "impodo_test.categories_2", "product.category", 10
            ),
            "impodo_test.products_3": ExternalIdBinding(
                "impodo_test.products_3", "product.template", 11
            ),
        }

    def read_ids(self, model, identifiers, fields):
        return tuple(
            ReadbackRecord(
                identifier,
                {field: self.records[(model, identifier)][field] for field in fields},
            )
            for identifier in identifiers
            if (model, identifier) in self.records
        )

    def find_records(self, model, domain, fields):
        if model in self.references and domain:
            identifier = self.references[model].get(domain[0][2])
            if identifier is not None:
                return (ReadbackRecord(identifier, {}),)
        del fields
        return self.uncertain

    def read_external_ids(self, external_ids):
        return tuple(
            self.external_ids[item]
            for item in external_ids
            if item in self.external_ids
        )


def _run(snapshot, statuses=None):
    identifiers = (10, 11, 50)
    rows = tuple(
        ExecutionRowAttempt(
            row_id=row.row_id,
            dataset=row.dataset,
            source_row=row.source_row,
            target_model=row.target_model,
            operation=row.disposition,
            field_names=tuple(intent.field for intent in row.fields),
            proposed_external_id=row.proposed_external_id,
            status=(statuses[index] if statuses else ExecutionRowStatus.COMMITTED),
            attempt=(0 if statuses and statuses[index] is ExecutionRowStatus.BLOCKED else 1),
            odoo_id=(
                identifiers[index]
                if not statuses or statuses[index] is ExecutionRowStatus.COMMITTED
                else None
            ),
            safe_error=(
                "Not attempted after an uncertain response"
                if statuses and statuses[index] is ExecutionRowStatus.BLOCKED
                else ""
            ),
        )
        for index, row in enumerate(snapshot.rows)
    )
    return ExecutionRun(
        run_id=str(uuid4()),
        project_id=snapshot.project_id,
        snapshot_hash=snapshot.semantic_hash,
        snapshot_root_hash=snapshot.root_hash,
        preflight_run_id=snapshot.preflight_run_id,
        target_hash=snapshot.target_hash,
        target_database=snapshot.target_database,
        status=(
            ExecutionRunStatus.OUTCOME_UNKNOWN
            if statuses and ExecutionRowStatus.OUTCOME_UNKNOWN in statuses
            else ExecutionRunStatus.COMPLETED
        ),
        started_at=datetime.now(timezone.utc),
        started_by="Local operator",
        completed_at=datetime.now(timezone.utc),
        rows=rows,
    )


class ReconciliationServiceTests(unittest.TestCase):
    def _service(self, snapshot, run):
        results = _Results()
        service = ReconciliationService(
            SimpleNamespace(
                execution_snapshot=lambda project_id, preflight_id: (
                    snapshot
                    if (project_id, preflight_id)
                    == (snapshot.project_id, snapshot.preflight_run_id)
                    else None
                )
            ),
            _Execution(run),
            results,
            CapabilityAuthorizationPolicy(),
        )
        return service, results

    def test_reads_committed_rows_back_and_verifies_relationship_ids(self):
        snapshot = _snapshot()
        run = _run(snapshot)
        service, results = self._service(snapshot, run)

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=_Reader(execution_api_scope(snapshot).semantic_hash),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.status, ReconciliationRunStatus.VERIFIED)
        self.assertEqual(report.verified_count, 3)
        self.assertTrue(
            all(row.status is ReconciliationRowStatus.VERIFIED for row in report.rows)
        )
        self.assertEqual(results.report.semantic_hash, report.semantic_hash)

    def test_unknown_create_is_rematched_without_retrying_the_write(self):
        snapshot = _snapshot()
        run = _run(
            snapshot,
            (
                ExecutionRowStatus.OUTCOME_UNKNOWN,
                ExecutionRowStatus.BLOCKED,
                ExecutionRowStatus.BLOCKED,
            ),
        )
        service, _results = self._service(snapshot, run)
        reader = _Reader(execution_api_scope(snapshot).semantic_hash)
        reader.uncertain = (ReadbackRecord(10, {"name": "Category"}),)

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.rows[0].status, ReconciliationRowStatus.VERIFIED)
        self.assertEqual(report.rows[0].odoo_id, 10)
        self.assertEqual(report.status, ReconciliationRunStatus.FALLOUT)

    def test_absent_unknown_create_is_explicitly_safe_to_plan_again(self):
        snapshot = _snapshot()
        run = _run(
            snapshot,
            (
                ExecutionRowStatus.OUTCOME_UNKNOWN,
                ExecutionRowStatus.BLOCKED,
                ExecutionRowStatus.BLOCKED,
            ),
        )
        service, _results = self._service(snapshot, run)

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=_Reader(execution_api_scope(snapshot).semantic_hash),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.rows[0].status, ReconciliationRowStatus.NOT_APPLIED)
        self.assertTrue(report.rows[0].retry_safe)
        self.assertEqual(report.unknown_count, 0)

    def test_reports_only_differing_field_names(self):
        snapshot = _snapshot()
        run = _run(snapshot)
        service, _results = self._service(snapshot, run)
        reader = _Reader(execution_api_scope(snapshot).semantic_hash)
        reader.records[("res.partner", 50)]["email"] = "different@example.test"

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        contact = report.rows[-1]
        self.assertEqual(contact.status, ReconciliationRowStatus.DIFFERENT)
        self.assertEqual(contact.differing_fields, ("email",))
        self.assertNotIn("different@example.test", report.to_json())

    def test_reports_a_missing_external_id_as_fallout(self):
        snapshot = _snapshot()
        run = _run(snapshot)
        service, _results = self._service(snapshot, run)
        reader = _Reader(execution_api_scope(snapshot).semantic_hash)
        del reader.external_ids[snapshot.rows[0].proposed_external_id]

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.status, ReconciliationRunStatus.FALLOUT)
        self.assertEqual(report.rows[0].status, ReconciliationRowStatus.DIFFERENT)
        self.assertEqual(report.rows[0].differing_fields, ("External ID",))

    def test_reports_an_external_id_bound_to_another_record_as_fallout(self):
        snapshot = _snapshot()
        run = _run(snapshot)
        service, _results = self._service(snapshot, run)
        reader = _Reader(execution_api_scope(snapshot).semantic_hash)
        external_id = snapshot.rows[1].proposed_external_id
        reader.external_ids[external_id] = ExternalIdBinding(
            external_id,
            "product.template",
            999,
        )

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.rows[1].status, ReconciliationRowStatus.DIFFERENT)
        self.assertEqual(report.rows[1].differing_fields, ("External ID",))

    def test_verifies_a_remote_create_link_to_an_existing_target_record(self):
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
        scoped = replace(
            snapshot,
            rows=(snapshot.rows[0], product, snapshot.rows[2]),
        )
        run = _run(scoped)
        service, _results = self._service(scoped, run)
        reader = _Reader(execution_api_scope(scoped).semantic_hash)
        reader.records[("product.template", 11)]["categ_id"] = [
            50,
            "Existing Category",
        ]
        reader.references["product.category"] = {"Existing Category": 50}

        report = service.reconcile(
            scoped.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.status, ReconciliationRunStatus.VERIFIED)
        self.assertEqual(report.rows[1].status, ReconciliationRowStatus.VERIFIED)

    def test_verifies_remote_many2many_imported_and_existing_members(self):
        snapshot = _remote_many2many_snapshot()
        run = _run(snapshot)
        service, _results = self._service(snapshot, run)
        reader = _Reader(execution_api_scope(snapshot).semantic_hash)
        reader.records[("product.template", 11)] = {
            "default_code": "P1",
            "name": "Product",
            "x_category_ids": [50, 10],
        }
        reader.references["product.category"] = {"Existing Category": 50}

        report = service.reconcile(
            snapshot.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.status, ReconciliationRunStatus.VERIFIED)
        self.assertEqual(report.rows[1].status, ReconciliationRowStatus.VERIFIED)

    def test_verifies_schema_bound_custom_many2many_fields(self):
        snapshot = _snapshot()
        contact = replace(
            snapshot.rows[-1],
            fields=(
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
        scoped = replace(snapshot, rows=(*snapshot.rows[:-1], contact))
        run = _run(scoped)
        service, _results = self._service(scoped, run)
        reader = _Reader(execution_api_scope(scoped).semantic_hash)
        reader.records[("res.partner", 50)] = {"x_tag_ids": [61, 60]}
        reader.references["x.tag"] = {"BLUE": 60, "FOOD": 61}

        report = service.reconcile(
            scoped.project_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.rows[-1].status, ReconciliationRowStatus.VERIFIED)


class Json2ReadbackReaderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def transport(url, headers, body, timeout, method):
            payload = json.loads(body)
            self.calls.append((url, headers, payload, timeout, method))
            return 200, [
                {
                    "id": 42,
                    **{
                        field: "Verified"
                        for field in payload["fields"]
                        if field != "id"
                    },
                }
            ]

        self.scope = OdooApiScope(
            preview_hash=HASH,
            models=(
                OdooModelScope(
                    "res.partner",
                    write_fields=("customer_rank", "name", "x_impodo_note"),
                    read_fields=("customer_rank", "name", "x_impodo_note"),
                    lookup_fields=("ref",),
                ),
            )
        )
        self.reader = Json2ReadbackReader(
            Json2Config(
                base_url="http://127.0.0.1:8069",
                database="odoo19_disposable",
                api_key="secret",
                connection_mode="LOCAL",
                retries=0,
            ),
            self.scope,
            transport=transport,
        )

    def test_reads_only_exact_ids_and_requested_fields(self):
        result = self.reader.read_ids("res.partner", (42,), ("name",))

        self.assertEqual(result[0].values, {"name": "Verified"})
        self.assertTrue(self.calls[0][0].endswith("/res.partner/search_read"))
        self.assertEqual(self.calls[0][2]["domain"], [["id", "in", [42]]])
        self.assertNotIn("secret", json.dumps(self.calls[0][2]))

        custom = self.reader.read_ids(
            "res.partner",
            (42,),
            ("customer_rank", "x_impodo_note"),
        )
        self.assertEqual(
            set(custom[0].values),
            {"customer_rank", "x_impodo_note"},
        )

    def test_rejects_broad_or_out_of_scope_reads(self):
        with self.assertRaises(OdooReadbackError):
            self.reader.find_records("res.partner", (), ("name",))
        with self.assertRaises(OdooReadbackError):
            self.reader.read_ids("res.partner", (42,), ("password",))

    def test_rejects_an_unrequested_record(self):
        def transport(*_args):
            return 200, [{"id": 99, "name": "Wrong"}]

        reader = replace(self.reader, transport=transport)
        with self.assertRaises(OdooReadbackError):
            reader.read_ids("res.partner", (42,), ("name",))

    def test_resolves_exact_external_ids_through_model_data(self):
        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            payload = json.loads(body)
            self.calls.append((url, payload))
            return 200, [
                {
                    "id": 7,
                    "module": "impodo_test",
                    "name": "partners_42",
                    "model": "res.partner",
                    "res_id": 42,
                }
            ]

        reader = replace(self.reader, transport=transport)
        result = reader.read_external_ids(("impodo_test.partners_42",))

        self.assertEqual(
            result,
            (
                ExternalIdBinding(
                    "impodo_test.partners_42",
                    "res.partner",
                    42,
                ),
            ),
        )
        self.assertTrue(self.calls[-1][0].endswith("/ir.model.data/search_read"))
        self.assertEqual(
            self.calls[-1][1]["domain"],
            [
                ["module", "=", "impodo_test"],
                ["name", "in", ["partners_42"]],
            ],
        )

    def test_rejects_an_unrequested_external_id(self):
        def transport(*_args):
            return 200, [
                {
                    "id": 7,
                    "module": "other",
                    "name": "partners_42",
                    "model": "res.partner",
                    "res_id": 42,
                }
            ]

        reader = replace(self.reader, transport=transport)
        with self.assertRaises(OdooReadbackError):
            reader.read_external_ids(("impodo_test.partners_42",))


if __name__ == "__main__":
    unittest.main()
