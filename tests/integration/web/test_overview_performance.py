"""Bound overview reads without changing source evidence or owner lifetime."""

import asyncio
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.domain.shared.access import AuthorizationError
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements


class OverviewPerformanceTests(ProjectSetupBrowserTestCase):
    def test_overview_preserves_evidence_for_both_odoo_majors_and_access_modes(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, _, _ = self._mapping_ready_workspace(
                        scalar_field_count=2,
                        odoo_major=major,
                        connection_mode=mode,
                    )
                    context = self.app.state.context
                    selection = context.queries.get_source_selection(workspace_id)
                    schema = context.queries.get_odoo_schema_catalog(workspace_id)
                    for _ in range(2):
                        response = self._bounded_overview(workspace_id)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertIn("Six stages from source to Odoo", response.text)
                        self.assertIn("<h1>Large mapping</h1>", response.text)
                    self.assertEqual(
                        context.queries.get_source_selection(workspace_id),
                        selection,
                    )
                    self.assertEqual(
                        context.queries.get_odoo_schema_catalog(workspace_id),
                        schema,
                    )
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.readiness_calls, [])

    def test_owners_close_after_render_failure_and_denied_access_reads_no_page(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        connect = DuckDbConnectionFactory._connect_with_lock_wait
        owners = []

        def observe(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            owner = connect(factory, path, config)
            owners.append(owner)
            return owner

        with patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", observe):
            with (
                patch(
                    "impodo.web.routers.workspace_setup._render",
                    side_effect=ValueError("render failed"),
                ),
                self.assertRaisesRegex(ValueError, "render failed"),
            ):
                self.client.get(f"/workspaces/{workspace_id}/overview")
            self.assertTrue(owners)
            for owner in owners:
                with self.assertRaises(duckdb.ConnectionException):
                    owner.execute("SELECT 1")
            owners.clear()
            self.assertEqual(self._bounded_overview(workspace_id).status_code, 200)
            for owner in owners:
                with self.assertRaises(duckdb.ConnectionException):
                    owner.execute("SELECT 1")

        context = self.app.state.context
        with (
            patch.object(
                context.workspace_access,
                "resolve",
                side_effect=AuthorizationError("denied"),
            ),
            patch.object(
                context.navigation,
                "get_for_workspace",
                side_effect=AssertionError("page read before access"),
            ),
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(f"/workspaces/{workspace_id}/overview")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(timings.connection_count, 0)

    def test_workspace_landing_reads_off_loop_and_preserves_redirect(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context
        read = context.queries.get

        def off_loop(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return read(*args, **kwargs)

        with patch.object(context.queries, "get", side_effect=off_loop):
            response = self.client.get(
                f"/workspaces/{workspace_id}", follow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"], f"/workspaces/{workspace_id}/overview"
        )

    def _bounded_overview(self, workspace_id):
        with (
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/overview", follow_redirects=False
            )
        # Overview admission and rendering share the registry, linkage and
        # engine owners. Repeat requests reopen and validate all three.
        self.assertEqual(timings.connection_count, 3)
        self.assertEqual(timings.schema_check_count, 2)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), 31)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        return response
