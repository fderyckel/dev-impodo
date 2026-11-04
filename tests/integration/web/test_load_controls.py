"""Exercise explicit pause continuation and the authenticated stop command."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.execution.load_control import LoadControl, RejectionPolicy
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus
from impodo.domain.execution.write_diagnostics import OdooWriteDiagnostic, WriteFailureCode
from tests.integration.web import test_load_recovery as fixtures
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.identity import resolve_local_actor


class LoadControlBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LoadRecoveryBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_paused_choice_binds_current_control_and_changed_pause_needs_recheck(self):
        fixture = self.fixture
        control = LoadControl(policy=RejectionPolicy.CONTINUE)
        for _ in range(10):
            control = control.settle("res.partner", "CREATE", rejected=True)
        fixture.run.load_control = control
        fixture.run.rejected_count = 10
        fixture.recovery_plan.load_control_hash = control.content_hash
        with fixture.recovery_context() as mocks:
            page = fixture.client.get(
                f"/workspaces/{fixture.workspace.workspace_id}/load/outcome"
            )
            self.assertIn("Paused after repeated failures", page.text)
            self.assertIn("Stop further uploads", page.text)
            fixture.check()
            page = fixture.client.get(
                f"/workspaces/{fixture.workspace.workspace_id}/load/outcome"
            )
            self.assertIn("Continue with the next small group", page.text)
            fixture.run.load_control = replace(control, isolation_calls=1)
            response = fixture.submit()
            self.assertEqual(response.status_code, 422)
            mocks["writer"].assert_not_called()

    def test_stop_runs_off_loop_with_bounded_owners_and_rejects_bad_csrf(self):
        fixture = self.fixture

        def stop(workspace_id, run_id, *, actor):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertEqual(workspace_id, fixture.workspace.workspace_id)
            self.assertEqual(run_id, fixture.run.run_id)
            self.assertEqual(actor, resolve_local_actor(fixture.context))
            fixture.context.queries.get(workspace_id)
            return LoadControl().request_stop()

        with patch.object(
            type(fixture.context.execution), "request_stop", side_effect=stop
        ) as command:
            url = f"/workspaces/{fixture.workspace.workspace_id}/load/stop"
            invalid = fixture.client.post(
                url,
                data={"csrf_token": "bad", "execution_run_id": fixture.run.run_id},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(invalid.status_code, 403)
            command.assert_not_called()
            with collect_duckdb_request_timings() as timings:
                response = fixture.client.post(
                    url,
                    data={
                        "csrf_token": fixture.csrf,
                        "execution_run_id": fixture.run.run_id,
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
            self.assertEqual(response.status_code, 303, response.text)
            command.assert_called_once()
            # Two navigation-guard owners precede one local command scope;
            # that scope retains registry, workspace, engine and Data version.
            self.assertLessEqual(timings.connection_count, 6)
            self.assertLessEqual(timings.schema_check_count, 1)

    def test_paused_outcome_pages_saved_rejections_without_loading_again(self):
        fixture = self.fixture
        control = LoadControl(policy=RejectionPolicy.CONTINUE).settle_many(
            ("res.partner", "CREATE", True) for _ in range(21)
        )
        fixture.run.load_control = control
        fixture.run.rejected_count = 21
        row = ExecutionRowAttempt(
            "contact", "customers", 2, "res.partner", "CREATE", ("name", "vat"),
            "test.contact", status=ExecutionRowStatus.FAILED,
            diagnostic=OdooWriteDiagnostic(
                WriteFailureCode.VALIDATION, "res.partner", "load", fields=("vat",),
            ),
        )
        fixture.run.rows = (
            replace(row, dataset="payment terms", status=ExecutionRowStatus.COMMITTED,
                    diagnostic=None, odoo_id=42),
            *(replace(row, row_id=f"contact-{i}", source_row=i) for i in range(2, 23)),
            replace(row, dataset="waiting customers", status=ExecutionRowStatus.PLANNED,
                    diagnostic=None),
        )
        url = f"/workspaces/{fixture.workspace.workspace_id}/load/outcome"
        for version in ("19.0", "20.0"):
            with self.subTest(version=version), fixture.recovery_context() as mocks:
                fixture.snapshot.target_odoo_version = version
                with collect_duckdb_request_timings() as timings:
                    page = fixture.client.get(url)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn('href="#row-outcomes">Review rejected records', page.text)
                self.assertIn("Odoo rejected a field value. Fields: vat.", page.text)
                self.assertIn("Showing 1-20 of 21 records.", page.text)
                self.assertNotIn("Payment Terms", page.text)
                self.assertNotIn("Waiting Customers", page.text)
                next_page = fixture.client.get(url, params={"rows_page": 2})
                self.assertIn("Showing 21-21 of 21 records.", next_page.text)
                mocks["writer"].assert_not_called()
                self.assertLessEqual(timings.connection_count, 6)
                self.assertLessEqual(timings.schema_check_count, 3)
