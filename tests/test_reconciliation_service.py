from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
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
from impodo.odoo_readback import (
    Json2ReadbackReader,
    OdooReadbackError,
    ReadbackRecord,
)

from tests.test_execution_service import TARGET_HASH, _snapshot


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

    def __init__(self):
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
        del model, domain, fields
        return self.uncertain


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
            reader=_Reader(),
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
        reader = _Reader()
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
            reader=_Reader(),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(report.rows[0].status, ReconciliationRowStatus.NOT_APPLIED)
        self.assertTrue(report.rows[0].retry_safe)
        self.assertEqual(report.unknown_count, 0)

    def test_reports_only_differing_field_names(self):
        snapshot = _snapshot()
        run = _run(snapshot)
        service, _results = self._service(snapshot, run)
        reader = _Reader()
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


class Json2ReadbackReaderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def transport(url, headers, body, timeout, method):
            self.calls.append((url, headers, json.loads(body), timeout, method))
            return 200, [{"id": 42, "name": "Verified"}]

        self.reader = Json2ReadbackReader(
            Json2Config(
                base_url="http://127.0.0.1:8069",
                database="odoo19_disposable",
                api_key="secret",
                connection_mode="LOCAL",
                retries=0,
            ),
            transport=transport,
        )

    def test_reads_only_exact_ids_and_requested_fields(self):
        result = self.reader.read_ids("res.partner", (42,), ("name",))

        self.assertEqual(result[0].values, {"name": "Verified"})
        self.assertTrue(self.calls[0][0].endswith("/res.partner/search_read"))
        self.assertEqual(self.calls[0][2]["domain"], [["id", "in", [42]]])
        self.assertNotIn("secret", json.dumps(self.calls[0][2]))

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


if __name__ == "__main__":
    unittest.main()
