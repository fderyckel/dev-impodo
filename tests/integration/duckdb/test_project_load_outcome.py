"""Project results use receipts from the selected run and its exact company."""

from unittest import TestCase

import duckdb

from impodo.adapters.duckdb.project_load_outcome import read_project_load_outcome
from impodo.adapters.duckdb.schema.execution import create_execution_schema
from impodo.adapters.duckdb.schema.odoo_company import create_odoo_company_bindings
from impodo.adapters.duckdb.schema.reconciliation import create_reconciliation_schema
from impodo.domain.odoo.company import OdooCompanySelection
from tests.support.database_probe import StatementCountingConnection


class ProjectLoadOutcomeTests(TestCase):
    def test_counts_and_company_are_bound_to_one_run(self):
        with duckdb.connect(":memory:") as db:
            create_execution_schema(db)
            create_reconciliation_schema(db)
            create_odoo_company_bindings(db)
            company = OdooCompanySelection("sha256:" + "1" * 64, 2, "North")
            other = OdooCompanySelection(company.target_hash, 3, "South")
            db.executemany(
                "INSERT INTO odoo_company_binding VALUES (?, ?)",
                [(c.binding_hash, c.to_json()) for c in (company, other)],
            )
            db.executemany(
                """INSERT INTO execution_run
                   (run_id, snapshot_hash, snapshot_root_hash, preflight_run_id,
                    target_hash, target_database, status, started_at, started_by, write_context_hash)
                   VALUES (?, 'snapshot', 'root', 'review', ?, 'destination', 'COMPLETED_WITH_ERRORS',
                           '2026-11-10', 'operator', ?)""",
                [(run, company.target_hash, company.context_hash) for run in ("old", "current")],
            )
            db.execute("INSERT INTO execution_current VALUES (1, 'current')")
            statuses = ["COMMITTED", "PARTIALLY_APPLIED", "FAILED", "OUTCOME_UNKNOWN",
                        "IN_FLIGHT", "BLOCKED", "PLANNED", "RETRY_READY"]
            db.executemany(
                """INSERT INTO execution_row
                    VALUES (?, ?, ?, 'Items', 'product.template', 'CREATE', ?, 0, NULL, '', 'unused', '2026-11-10')""",
                [(run, i, str(i), status) for run in ("old", "current") for i, status in enumerate(statuses)],
            )
            queries = []
            connection = StatementCountingConnection(db, queries)
            outcome = read_project_load_outcome(
                connection, "current", company_binding_hash=company.binding_hash,
                read_context_hash="", include_counts=True,
            )
            self.assertEqual(outcome.company_name, "North")
            self.assertEqual(outcome.database, "destination")
            self.assertEqual(outcome.detail, "1 written · 1 rejected · 2 not attempted · "
                             "2 outcome uncertain · 1 partially applied · 1 awaiting retry")
            self.assertIsNone(outcome.verified_at)
            self.assertEqual(len(queries), 2)
            queries.clear()
            foreign = read_project_load_outcome(
                connection, "current", company_binding_hash=other.binding_hash,
                read_context_hash="", include_counts=False,
            )
            self.assertEqual(foreign.company_name, "")
            self.assertEqual(foreign.detail, "")
            self.assertEqual(len(queries), 1)
            self.assertIsNone(read_project_load_outcome(
                connection, "old", company_binding_hash=company.binding_hash,
                read_context_hash="", include_counts=True,
            ))
