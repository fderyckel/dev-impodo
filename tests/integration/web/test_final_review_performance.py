"""Bound action-rich Final review reads and release stores before worker dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class FinalReviewPerformanceTests(HierarchyWorkflowFixture):
    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(self.context.queries.get_odoo_schema_catalog(workspace_id))
        # Ambiguous existing parents leave real dependency groups to review.
        target.records[("product.category", 900)] = {"name": "Machines", "parent_id": False}
        report = run_local_operation_sync(
            self.context.preflight.compare, workspace_id, reader=target.compare, actor=self.actor,
        )
        requests = len(target.requests)
        self.assertNotEqual(report.status, "READY")
        prefix = f"/workspaces/{workspace_id}/summary"
        for path, expected, opens, checks, limit in (
            (prefix, "Preview all problem groups", 4, 3, 165),
            (prefix + "/deferred-groups", "Preview all problem groups", 6, 4, 100),
        ):
            with self.subTest(path=path):
                page = self.bounded(lambda: self.client.get(path), opens, checks, limit)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn(expected, page.text)

        preview = self.bounded(lambda: self._post(prefix + "/deferred-groups/preview", {
            "csrf_token": self.csrf, "select_all": "1",
        }), 6, 4, 170)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("Set aside all problem groups", preview.text)
        self.assertNotIn('name="issue_id"', preview.text)

        def enqueue(*args, **kwargs):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertEqual(args[0], workspace_id)
            self.assertEqual(kwargs["workspace"].workspace_id, workspace_id)
            return SimpleNamespace(job_id="isolated-dispatch", active=True)

        with patch.object(self.context.preparation_jobs, "enqueue", side_effect=enqueue) as dispatch:
            queued = self.bounded(lambda: self._post(prefix + "/check", {
                "csrf_token": self.csrf,
            }), 8, 6, 85)
        self.assertEqual(queued.status_code, 303)
        dispatch.assert_called_once()
        self.assertEqual(len(target.requests), requests)
        self.assertEqual(target.writes, [])
        self.assertEqual(self.context.preflight.current_report(workspace_id), report)

    def bounded(self, callback, opens, schema_checks, maximum_statements):
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            response = callback()
        self.assertLessEqual(timings.connection_count, opens)
        self.assertLessEqual(timings.schema_check_count, schema_checks)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), maximum_statements)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        return response
