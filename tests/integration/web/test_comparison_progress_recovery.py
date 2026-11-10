"""Keep failed comparison pages and polled recovery actions consistent."""

import asyncio
import time
from html.parser import HTMLParser
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.odoo_read_failures import (
    OdooReadFailureCode,
    OdooReadWorkflowError,
)
from impodo.application.preflight_jobs import PreflightJobManager
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorTransportError,
)
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.source_forms import hidden_source_form_value


class ProgressControls(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.controls = {}
        self.in_details = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "details":
            self.in_details = True
        for key in attributes:
            if key.startswith("data-preflight-"):
                self.controls[key] = (tag, attributes, self.in_details)

    def handle_endtag(self, tag):
        if tag == "details":
            self.in_details = False


class ComparisonProgressRecoveryTests(ProjectSetupBrowserTestCase):
    def exercise(
        self,
        failure,
        *,
        previous=False,
        lookup_failure=False,
        published=False,
        publication_started=False,
    ):
        context = self.app.state.context
        workspace, _ = self._frozen_odoo_source_workspace()
        manager = PreflightJobManager()
        context.preflight_jobs = manager
        self.addCleanup(manager.shutdown)
        started = Event()
        release = Event()
        operation_ids = []
        self.addCleanup(release.set)

        def compare(_workspace_id, **kwargs):
            operation_ids.append(kwargs["operation_id"])
            kwargs["progress"]("PUBLISHING" if publication_started else "READING")
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("Comparison fixture was not released")
            if failure is not None:
                raise failure
            return SimpleNamespace(run_id="saved-review", attention_count=0)

        original_report = context.preflight.current_report
        read_threads = []

        def current_report(workspace_id):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            read_threads.append(True)
            if lookup_failure:
                raise OSError("unavailable previous evidence")
            if published:
                return SimpleNamespace(
                    run_id="new-review", operation_id=operation_ids[0]
                )
            if previous:
                return SimpleNamespace(
                    run_id="previous-review", operation_id="previous-attempt"
                )
            return original_report(workspace_id)

        with (
            patch.object(context.preflight, "compare", side_effect=compare),
            patch.object(
                context.preflight,
                "comparison_input_hash",
                return_value="sha256:" + "1" * 64,
            ),
            patch(
                "impodo.web.routers.preflight._rebind_remote_read_access",
                return_value=None,
            ),
            patch.object(
                context.preflight, "current_report", side_effect=current_report
            ) as report_read,
        ):
            response = self.client.post(
                f"/workspaces/{workspace.workspace_id}/summary/compare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303, response.text)
            self.assertTrue(started.wait(timeout=5))
            url = response.headers["location"]
            running = self.client.get(url)
            self.assertEqual(running.status_code, 200, running.text)
            controls = ProgressControls(running.text).controls
            self.assertIn("data-preflight-failure-action", controls)
            self.assertIn("data-preflight-retry", controls)
            self.assertNotIn("hidden", controls["data-preflight-progress-card"][1])
            report_read.assert_not_called()
            release.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status_response = self.client.get(url + "/status")
                self.assertEqual(status_response.status_code, 200, status_response.text)
                status = status_response.json()
                if status["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.005)
            else:
                self.fail("Comparison fixture did not finish")
            with (
                count_duckdb_statements() as statements,
                collect_duckdb_request_timings() as timings,
            ):
                page = self.client.get(url)
            self.assertEqual(page.status_code, 200, page.text)
            # Parent admission uses registry and engine owners. The complete
            # progress projection opens each of those paths at most once again.
            self.assertLessEqual(timings.connection_count, 4)
            self.assertLessEqual(timings.schema_check_count, 3)
            self.assertLessEqual(len(statements), 65)
            self.assertEqual(timings.lock_retry_count, 0)
            if failure is None:
                report_read.assert_not_called()
            else:
                self.assertTrue(read_threads)
        return workspace.workspace_id, page, status, running

    def test_stale_access_stops_progress_and_opens_odoo_details(self):
        workspace_id, page, status, _ = self.exercise(
            OdooReadWorkflowError(
                OdooReadFailureCode.SCHEMA_EVIDENCE_STALE, "raw mismatch"
            ),
            previous=True,
        )
        controls = ProgressControls(page.text).controls
        self.assertIn("Comparison stopped", page.text)
        self.assertIn("No new comparison was saved.", page.text)
        self.assertNotIn("The comparison result is saved.", page.text)
        self.assertNotIn("while the read-only comparison continues", page.text)
        self.assertIn("hidden", controls["data-preflight-progress-card"][1])
        action = controls["data-preflight-failure-action"][1]
        self.assertNotIn("hidden", action)
        self.assertEqual(action["href"], f"/workspaces/{workspace_id}/schema")
        self.assertEqual(status["failure_action_href"], action["href"])
        self.assertEqual(status["failure_action_label"], "Review Odoo details")
        self.assertEqual(status["failure_action_kind"], "link")
        self.assertNotIn("hidden", controls["data-preflight-previous-review"][1])
        self.assertTrue(status["previous_review_available"])
        self.assertTrue(controls["data-preflight-failure-code"][2])
        details = self.client.get(action["href"])
        self.assertEqual(details.status_code, 200, details.text)
        self.assertIn("Check for Odoo changes", details.text)

    def test_first_failure_does_not_offer_a_previous_review(self):
        _, page, status, _ = self.exercise(
            OdooReadWorkflowError(
                OdooReadFailureCode.SCHEMA_EVIDENCE_STALE, "raw mismatch"
            )
        )
        self.assertFalse(status["previous_review_available"])
        self.assertIn(
            "hidden",
            ProgressControls(page.text).controls["data-preflight-previous-review"][1],
        )

    def test_retry_keeps_a_post_form_and_uses_a_new_operation(self):
        workspace_id, page, status, running = self.exercise(
            ConnectorTransportError("unreachable")
        )
        controls = ProgressControls(page.text).controls
        action = f"/workspaces/{workspace_id}/summary/compare"
        self.assertEqual(controls["data-preflight-retry"][1]["method"], "post")
        self.assertNotIn("hidden", controls["data-preflight-retry"][1])
        self.assertIn("hidden", controls["data-preflight-failure-action"][1])
        self.assertEqual(
            hidden_source_form_value(page.text, action, "csrf_token"), self.csrf
        )
        old_operation = hidden_source_form_value(running.text, action, "operation_id")
        new_operation = hidden_source_form_value(page.text, action, "operation_id")
        self.assertNotEqual(old_operation, new_operation)
        self.assertNotEqual(old_operation, status["comparison_operation_id"])
        UUID(status["comparison_operation_id"])
        self.assertEqual(status["failure_action_kind"], "retry")
        self.assertEqual(status["failure_action_href"], action)

    def test_key_recovery_returns_to_the_protected_final_review_form(self):
        workspace_id, page, status, _ = self.exercise(
            ConnectorAuthenticationError("rejected")
        )
        action = ProgressControls(page.text).controls["data-preflight-failure-action"][
            1
        ]
        self.assertEqual(action["href"], f"/workspaces/{workspace_id}/summary")
        self.assertEqual(status["failure_action_href"], action["href"])
        self.assertEqual(status["failure_action_label"], "Return to final review")
        self.assertEqual(status["failure_action_kind"], "credential")

    def test_success_still_opens_the_saved_review(self):
        workspace_id, page, status, _ = self.exercise(None)
        controls = ProgressControls(page.text).controls
        self.assertIn("Comparison saved", page.text)
        self.assertIn("The comparison result is saved.", page.text)
        self.assertNotIn("while the read-only comparison continues", page.text)
        self.assertIn("hidden", controls["data-preflight-failed"][1])
        self.assertNotIn("hidden", controls["data-preflight-complete"][1])
        self.assertEqual(
            status["redirect_url"], f"/workspaces/{workspace_id}/load/review"
        )

    def test_storage_failure_does_not_claim_whether_publication_finished(self):
        _, page, status, _ = self.exercise(
            SecretStoreError("unavailable local storage"),
            lookup_failure=True,
        )
        self.assertIn("Impodo could not confirm the comparison result.", page.text)
        self.assertNotIn("No new comparison was saved.", page.text)
        self.assertNotIn("The comparison result is saved.", page.text)
        self.assertEqual(
            status["outcome_message"], "Impodo could not confirm the comparison result."
        )
        self.assertFalse(status["previous_review_available"])
        self.assertEqual(status["failure_action_label"], "Return to final review")

    def test_a_saved_comparison_is_reported_when_a_later_step_fails(self):
        _, page, status, _ = self.exercise(
            ConnectorTransportError("later Recipe verification failed"),
            published=True,
        )
        self.assertIn(
            "A new comparison was saved, but a later step needs attention.", page.text
        )
        self.assertNotIn("No new comparison was saved.", page.text)
        self.assertFalse(status["previous_review_available"])
        self.assertIn(
            "hidden",
            ProgressControls(page.text).controls["data-preflight-previous-review"][1],
        )

    def test_failure_after_publication_started_does_not_assume_no_save(self):
        _, page, status, _ = self.exercise(
            ConnectorTransportError("later step failed"),
            publication_started=True,
            lookup_failure=True,
        )
        self.assertIn("Impodo could not confirm the comparison result.", page.text)
        self.assertNotIn("No new comparison was saved.", page.text)
        self.assertFalse(status["previous_review_available"])
