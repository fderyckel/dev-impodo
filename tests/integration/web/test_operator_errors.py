"""Review private saved errors through the authorized outcome page."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus
from impodo.domain.execution.operator_errors import (
    OperatorErrorReceipt,
    capture_operator_error,
)
from impodo.domain.shared.access import Actor, ActorIdentity, Capability
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class OperatorErrorBrowserTests(ProjectSetupBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.context = self.app.state.context
        self.workspace = self.workspaces.create(
            name="Fictional rejected load", source_system="Other"
        )
        self.row = ExecutionRowAttempt(
            "saved-row",
            "customers",
            90,
            "res.partner",
            "CREATE",
            ("name",),
            "synthetic.row",
            status=ExecutionRowStatus.FAILED,
        )
        self.url = f"/workspaces/{self.workspace.workspace_id}/load/errors/11111111-1111-4111-8111-111111111111/{self.row.row_id}"

    def test_saved_explanation_is_escaped_and_reopening_does_not_read_current_source(
        self,
    ):
        detail = capture_operator_error(
            [
                {
                    "type": "error",
                    "message": "Fictional <script>rule</script> rejected wire-value",
                    "field": "name",
                }
            ],
            ({"name": "wire-value"},),
        )
        receipt = OperatorErrorReceipt("sha256:" + "1" * 64, (self.row.row_id,), detail)

        def read(*_):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return self.row, receipt

        with (
            patch.object(
                type(self.context.execution.journal),
                "operator_error_for_row",
                side_effect=read,
            ),
            patch.object(
                type(self.context.execution.preflight),
                "current_execution_snapshot",
                side_effect=AssertionError("Must use saved attempt"),
            ),
        ):
            for _ in range(2):
                with collect_duckdb_request_timings() as timings:
                    page = self.client.get(self.url)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertEqual(page.headers["cache-control"], "no-store")
                self.assertIn("Customers · row 90", page.text)
                self.assertIn(
                    "Fictional &lt;script&gt;rule&lt;/script&gt; rejected wire-value",
                    page.text,
                )
                self.assertNotIn("Fictional <script>", page.text)
                self.assertIn("Reported by Odoo for this record", page.text)
                self.assertIn(
                    f"/workspaces/{self.workspace.workspace_id}/mapping", page.text
                )
                self.assertLessEqual(timings.connection_count, 6)
                self.assertLessEqual(timings.schema_check_count, 4)

    def test_unspecified_field_batch_membership_and_historical_absence_are_honest(self):
        detail = capture_operator_error(
            [
                {
                    "type": "error",
                    "message": "Fictional record rule",
                    "rows": {"from": 1, "to": 1},
                    "field": "name",
                }
            ],
            ({"name": "first"}, {"name": "second"}),
        )
        receipt = OperatorErrorReceipt(
            "sha256:" + "1" * 64, (self.row.row_id, "other"), detail
        )
        read_type = type(self.context.execution.journal)
        with patch.object(
            read_type, "operator_error_for_row", return_value=(self.row, receipt)
        ):
            page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("rejected as a group", page.text)
        self.assertIn("Submitted context", page.text)
        self.assertNotIn("Reported by Odoo for this record", page.text)
        self.assertIn("first", page.text)
        self.assertNotIn("<td>second</td>", page.text)
        detail = capture_operator_error(
            [{"type": "error", "message": "Fictional record rule"}],
            ({"name": "first"},),
        )
        with patch.object(
            read_type,
            "operator_error_for_row",
            return_value=(
                self.row,
                replace(receipt, row_ids=(self.row.row_id,), detail=detail),
            ),
        ):
            page = self.client.get(self.url)
        self.assertIn("Odoo did not identify a field", page.text)
        with patch.object(
            read_type, "operator_error_for_row", return_value=(self.row, None)
        ):
            page = self.client.get(self.url)
        self.assertIn("no available private Odoo explanation", page.text)
        self.assertNotIn("Fictional record rule", page.text)

    def test_protected_value_authority_is_checked_before_private_repository_read(self):
        denied = Actor(
            ActorIdentity("test", "viewer", "Viewer"),
            frozenset({Capability.PROJECT_VIEW}),
        )
        with (
            patch.object(self.context.identity_resolver, "_actor", denied),
            patch.object(
                type(self.context.execution.journal), "operator_error_for_row"
            ) as read,
        ):
            page = self.client.get(self.url)
        self.assertEqual(page.status_code, 403, page.text)
        read.assert_not_called()

    def test_rejection_list_renders_one_page_and_keeps_next_attempt_binding(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        rows = tuple(
            (i, replace(self.row, row_id=f"saved-{i}", source_row=i + 90))
            for i in range(21)
        )
        with patch.object(
            type(self.context.execution.journal),
            "operator_error_rows",
            return_value=(run_id, rows),
        ) as read:
            with collect_duckdb_request_timings() as timings:
                page = self.client.get(
                    f"/workspaces/{self.workspace.workspace_id}/load/errors",
                    params={"run_id": run_id},
                )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.text.count(">Review Odoo error</a>"), 20)
        self.assertIn("after=19", page.text)
        self.assertIn(f"/load/errors/{run_id}/saved-0", page.text)
        self.assertNotIn(f"/load/errors/{run_id}/saved-20", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        read.assert_called_once_with(self.workspace.workspace_id, run_id, after=-1)

    def test_transfer_and_continuation_can_inspect_saved_errors_without_redirect(self):
        transfer = self.workspaces.create(
            name="Fictional transfer", source_system="Odoo", source_mode="ODOO"
        )
        with patch.object(
            type(self.context.execution.journal),
            "operator_error_for_row",
            return_value=(self.row, None),
        ):
            page = self.client.get(
                self.url.replace(self.workspace.workspace_id, transfer.workspace_id),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Review Destination matching", page.text)
        with (
            patch.object(
                self.context.corrections,
                "binding_for_successor",
                return_value=SimpleNamespace(current_plan=None),
            ),
            patch.object(
                type(self.context.execution.journal),
                "operator_error_for_row",
                return_value=(self.row, None),
            ),
        ):
            page = self.client.get(self.url, follow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)

    def test_recipe_policy_allows_saved_errors_without_redirecting_to_setup(self):
        # Exercise only routing with a synthetic Recipe access packet. Real
        # store linkage and protected authority are covered by separate tests.
        from fastapi.responses import HTMLResponse

        access = self.context.workspace_access.resolve(
            self.workspace.workspace_id,
            actor=resolve_local_actor(self.context),
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )
        with (
            patch.object(
                self.context.workspace_access,
                "resolve",
                return_value=replace(
                    access, run_purpose="TEST", recipe_application_id=str(uuid4())
                ),
            ),
            patch.object(
                type(self.context.execution),
                "operator_error",
                return_value=(self.row, None),
            ),
            patch.object(self.context.queries, "get", return_value=self.workspace),
            patch(
                "impodo.web.routers.execution._render",
                return_value=HTMLResponse("Saved error"),
            ),
        ):
            page = self.client.get(self.url, follow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)
