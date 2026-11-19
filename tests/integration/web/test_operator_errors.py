"""Review private saved errors through the authorized outcome page."""

import asyncio
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
from zipfile import ZipFile

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus
from impodo.domain.execution.operator_errors import (
    OperatorErrorReceipt,
    capture_operator_error,
)
from impodo.domain.shared.access import Actor, ActorIdentity, Capability
from impodo.web.diagnostics import LocalDiagnosticRecorder, create_diagnostic_bundle
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
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

    def test_explicit_diagnostic_download_includes_only_selected_rejection(self):
        recorder = LocalDiagnosticRecorder(Path(self.temporary.name) / "diagnostics")
        self.app.state.diagnostic_recorder = recorder
        self.addCleanup(recorder.close)
        row = replace(self.row, operation="UPDATE", field_names=("parent_id",))
        detail = capture_operator_error(
            [
                {"type": "error", "message": "Fictional company link rejected", "field": "parent_id", "rows": {"from": 0, "to": 0}},
                {"type": "error", "message": "Fictional batch rule"},
                {"type": "error", "message": "Peer-only explanation", "rows": {"from": 1, "to": 1}},
                {"type": "error", "message": "Traceback: protected internal exception"},
            ],
            ({"parent_id": 918273645, "token": "synthetic-secret"}, {"parent_id": 923456789}),
        )
        receipt = OperatorErrorReceipt("sha256:" + "1" * 64, (row.row_id, "other"), detail)
        run_id = "11111111-1111-4111-8111-111111111111"
        form = {"csrf_token": self.csrf, "workspace_id": self.workspace.workspace_id}
        selected = {**form, "run_id": run_id, "row_id": row.row_id}

        def build_bundle(*args, **kwargs):
            self.assertFalse(getattr(_read_databases, "connections", None))
            return create_diagnostic_bundle(*args, **kwargs)

        with (
            patch.object(
                type(self.context.execution.journal), "operator_error_for_row",
                return_value=(row, receipt),
            ) as read,
            patch("impodo.web.routers.lifecycle.create_diagnostic_bundle", side_effect=build_bundle),
        ):
            page = self.client.get(self.url)
            self.assertIn("Create diagnostic bundle with Odoo error", page.text)
            read.reset_mock()
            standard = self.client.post("/diagnostics/bundle", data=form, headers=POST_HEADERS)
            self.assertEqual(standard.status_code, 200, standard.text)
            missing_opt_in = self.client.post("/diagnostics/bundle", data=selected, headers=POST_HEADERS)
            self.assertEqual(missing_opt_in.status_code, 422)
            read.assert_not_called()
            with collect_duckdb_request_timings() as timings:
                response = self.client.post(
                    "/diagnostics/bundle",
                    data={**selected, "include_odoo_error": "1"},
                    headers=POST_HEADERS,
                )
            read.assert_called_once_with(self.workspace.workspace_id, run_id, row.row_id)
            self.assertLessEqual(timings.connection_count, 6)
            self.assertLessEqual(timings.schema_check_count, 6)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("-with-odoo-error.zip", response.headers["content-disposition"])
        member = "odoo-rejection.json"
        with ZipFile(BytesIO(standard.content)) as archive:
            self.assertNotIn(member, archive.namelist())
            for name in archive.namelist():
                self.assertNotIn(b"918273645", archive.read(name))
                self.assertNotIn(b"Fictional company link rejected", archive.read(name))
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertIn(member, archive.read("diagnosis.md").decode())
            self.assertEqual(
                json.loads(archive.read("manifest.json"))["load_evidence"]["private_rejection_member"],
                member,
            )
            error = json.loads(archive.read(member))
            self.assertEqual(error["execution_run_id"], run_id)
            self.assertEqual(error["source_row"], row.source_row)
            self.assertEqual(error["selected_batch_position"], 1)
            self.assertEqual(error["transport_batch_size"], 2)
            self.assertEqual(error["submitted_values_for_selected_record"], {"parent_id": "918273645", "token": "[protected]"})
            self.assertEqual(
                [(item["explanation"], item["reported_fields"], item["applies_to_selected_record"]) for item in error["odoo_messages"]],
                [("Fictional company link rejected", ["parent_id"], True), ("Fictional batch rule", [], False)],
            )
            self.assertTrue(error["detail_withheld"])
            for name in archive.namelist():
                encoded = archive.read(name)
                self.assertNotIn(b"Peer-only explanation", encoded)
                self.assertNotIn(b"923456789", encoded)
                self.assertNotIn(b"synthetic-secret", encoded)
                self.assertNotIn(b"protected internal exception", encoded)

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
            recorder = LocalDiagnosticRecorder(Path(self.temporary.name) / "diagnostics")
            self.app.state.diagnostic_recorder = recorder
            self.addCleanup(recorder.close)
            download = self.client.post(
                "/diagnostics/bundle",
                data={
                    "csrf_token": self.csrf, "include_odoo_error": "1",
                    "workspace_id": self.workspace.workspace_id,
                    "run_id": "11111111-1111-4111-8111-111111111111",
                    "row_id": self.row.row_id,
                },
                headers=POST_HEADERS,
            )
            self.assertEqual(download.status_code, 403, download.text)
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
