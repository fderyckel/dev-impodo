"""Exercise explicit pause continuation and the authenticated stop command."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.execution.load_control import LoadControl, RejectionPolicy
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
