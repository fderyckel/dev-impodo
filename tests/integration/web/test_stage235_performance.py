"""Bound complete page and action costs while preserving reviewed Odoo evidence."""

import asyncio
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.preflight_jobs import PreflightJobManager
from impodo.domain.workspace.business_keys import BUSINESS_KEY_POLICY_VERSION
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements


class Stage235PerformanceTests(ProjectSetupBrowserTestCase):
    def test_odoo19_local(self):
        self.exercise(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise(20, OdooConnectionMode.REMOTE)

    def exercise(self, major, mode):
        workspace_id, dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=2, odoo_major=major, connection_mode=mode,
        )
        context = self.app.state.context
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        original_selection = context.queries.get_source_selection(workspace_id)
        prefix = f"/workspaces/{workspace_id}/"

        for area, marker, opens, statements in (
            ("schema", "<title>Odoo data", 4, 70), ("mapping", "<title>Match data", 4, 120),
            ("summary", "<h1>Review and compare</h1>", 4, 80),
        ):
            with self.subTest(area=area):
                response = self.bounded(
                    lambda area=area: self.client.get(prefix + area, follow_redirects=False), opens, statements,
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(marker, response.text)

        manager = PreflightJobManager(attempt_store=context.migration_projects.repository)
        try:
            with patch.object(context, "preflight_jobs", manager):
                summary = self.bounded(lambda: self.client.get(prefix + "summary", follow_redirects=False), 4, 85)
                self.assertEqual(summary.status_code, 200)
        finally:
            manager.shutdown()

        selection_reader = context.queries.get_mapping_source_selection

        def off_loop(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return selection_reader(*args, **kwargs)

        with patch.object(context.queries, "get_mapping_source_selection", side_effect=off_loop):
            for formula, valid in (("'Ready'", True), ("__import__('os')", False)):
                response = self.bounded(lambda formula=formula: self._post(
                    prefix + "mapping/formula-validation",
                    {"csrf_token": self.csrf, "dataset_id": dataset.dataset_id, "formula": formula},
                ), 6, 40)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["valid"], valid)

        form = {
            "csrf_token": self.csrf, "expected_schema_hash": schema.content_hash,
            "expected_business_key_policy_version": str(BUSINESS_KEY_POLICY_VERSION),
            "primary_key_field_0": "ref", "key_description_0": "Unique reference",
        }
        confirmed = self.bounded(lambda: self._post(prefix + "schema/govern", form), 6, 100)
        self.assertEqual(confirmed.status_code, 303, confirmed.text)
        governance = context.queries.get_schema_governance(workspace_id)
        self.assertEqual(governance.business_keys[0].key_fields, ("ref",))
        rejected = self.bounded(lambda: self._post(
            prefix + "schema/govern", {**form, "primary_scope_field_0": "ref"},
        ), 6, 85)
        self.assertEqual(rejected.status_code, 422)
        self.assertIn("choose each field only once", rejected.text)
        self.assertEqual(context.queries.get_schema_governance(workspace_id), governance)
        self.assertEqual(context.queries.get_source_selection(workspace_id), original_selection)
        self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), schema)

        bad_order = self.bounded(lambda: self._post(prefix + "mapping/order", {
            "csrf_token": self.csrf, "action": "unsupported",
        }), 6, 130)
        self.assertEqual(bad_order.status_code, 422)
        self.assertIn("Choose a supported table-order action", bad_order.text)
        # A missing checked mapping must recover inside the command's owner
        # scope, including metadata, navigation and full editor rendering.
        impact = self.bounded(lambda: self._post(
            prefix + "mapping/transformation-impact/prepare", {"csrf_token": self.csrf},
        ), 6, 150)
        self.assertEqual(impact.status_code, 422)
        self.assertEqual(context.queries.get_source_selection(workspace_id), original_selection)
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.readiness_calls, [])

    def bounded(self, callback, opens, maximum_statements):
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            response = callback()
        # Complete page reads retain registry, linkage, engine and Data version
        # owners once; action routes keep their separate admission scopes.
        self.assertLessEqual(timings.connection_count, opens)
        self.assertLessEqual(timings.schema_check_count, 6)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), maximum_statements)
        catalog_reads = sum("duckdb_columns()" in statement for statement in statements)
        self.assertEqual(catalog_reads, timings.schema_check_count)
        return response
