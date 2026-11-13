"""Show recoverable read failures beside the action without losing saved work."""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from http.client import RemoteDisconnected
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.application.workspace.mapping.order_service import (
    _safe_matching_order_failure,
)
from impodo.domain.matching_order import (
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
)
from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorTransportError,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.diagnostics import LocalDiagnosticRecorder
from tests.application.workspace.execution import test_service as execution_fixtures
from tests.integration.web import test_load_diagnostics as diagnostics
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.required_values import required_values_workspace


class LoadDisconnectionMessageTests(unittest.TestCase):
    def test_lookup_failure_reaches_job_and_bundle_before_journal_or_writes(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for recipe in (False, True):
                    with (
                        self.subTest(major=major, mode=mode, recipe=recipe),
                        tempfile.TemporaryDirectory() as directory,
                        closing(LocalDiagnosticRecorder(directory)) as recorder,
                        patch("impodo.adapters.odoo.writer.time.sleep"),
                    ):
                        snapshot = replace(
                            execution_fixtures._snapshot(),
                            target_odoo_version=f"{major}.0",
                        )
                        service, journal = (
                            execution_fixtures.ExecutionServiceTests()._service(
                                snapshot, mode=mode
                            )
                        )
                        scope = execution_api_scope(snapshot)
                        executor = execution_fixtures._Executor(scope.semantic_hash)
                        calls = []

                        def transport(url, *_args):
                            calls.append(url)
                            raise RemoteDisconnected(
                                "private-server-text private-api-key"
                            )

                        writer = Json2WriteExecutor(
                            Json2Config(
                                base_url="http://127.0.0.1:8069"
                                if mode is OdooConnectionMode.LOCAL
                                else "https://odoo.example.test",
                                database=f"odoo{major}_test",
                                api_key="private-api-key",
                                connection_mode=mode.value,
                                retries=2,
                            ),
                            scope,
                            transport,
                        )
                        executor.find_ids_many = writer.find_ids_many

                        def work(*_args):
                            service.execute(
                                snapshot.workspace_id,
                                expected_snapshot_hash=snapshot.semantic_hash,
                                executor=executor,
                                actor=LOCAL_ACTOR,
                            )

                        job, _observer = diagnostics._run_job(
                            recorder, snapshot.workspace_id, work, recipe=recipe
                        )
                        members, attempts = diagnostics._bundle(recorder)
                        self.assertEqual(job.status.value, "FAILED")
                        self.assertIn(
                            "connection to Odoo was interrupted", job.failure_message
                        )
                        self.assertIn("before sending any records", job.failure_message)
                        self.assertIn("Check changes again", job.failure_message)
                        self.assertIsNone(journal.run)
                        self.assertEqual(executor.creates, [])
                        self.assertEqual(len(calls), 3)
                        self.assertTrue(
                            all(url.endswith("/search_read") for url in calls)
                        )
                        self.assertEqual(
                            attempts[0]["transport_observation"], "NOT_STARTED"
                        )
                        self.assertEqual(
                            attempts[0]["failure"]["causes"][0]["code"],
                            "LOAD_ODOO_IDENTITY_LOOKUP_UNAVAILABLE",
                        )
                        self.assertIn(
                            "read-only identity lookup could not finish",
                            members["diagnosis.md"],
                        )
                        self.assertNotIn("private-server-text", str(members))
                        self.assertNotIn("private-api-key", str(members))


class IdentityHealthFailureBrowserTests(ProjectSetupBrowserTestCase):
    def test_failed_read_only_check_can_be_retried_without_reentering_choices(self):
        workspace, _dataset, revision, _validation = required_values_workspace(self)
        context = self.app.state.context
        original_reader = context.readiness_reader

        def unavailable(*_args):
            raise ConnectorTransportError("private-server-text")

        def check():
            started = self.client.post(
                f"/workspaces/{workspace}/mapping/order/check",
                json={"entries": [["csrf_token", self.csrf]]},
                headers={**POST_HEADERS, "Accept": "application/json"},
            )
            self.assertEqual(started.status_code, 202, started.text)
            for _attempt in range(100):
                response = self.client.get(started.json()["status_url"])
                self.assertEqual(response.status_code, 200, response.text)
                if response.json()["terminal"]:
                    return response.json()
                time.sleep(0.02)
            self.fail("Read-only check did not reach a terminal state")

        context.readiness_reader = unavailable
        failed = check()
        self.assertEqual(failed["status"], "FAILED")
        page = self.client.get(
            f"/workspaces/{workspace}/mapping?order_check={failed['check_id']}"
        )
        self.assertIn(
            "connection to Odoo failed during the read-only identity check", page.text
        )
        self.assertEqual(
            context.queries.get_mapping_revision(workspace),
            revision,
        )

        context.readiness_reader = original_reader
        succeeded = check()
        self.assertEqual(succeeded["status"], "SUCCEEDED")
        page = self.client.get(
            f"/workspaces/{workspace}/mapping?order_check={succeeded['check_id']}"
        )
        self.assertNotIn(
            "connection to Odoo failed during the read-only identity check", page.text
        )
        self.assertEqual(
            context.queries.get_mapping_revision(workspace),
            revision,
        )

    def test_failed_check_shows_recovery_beside_retry_and_keeps_saved_mapping(self):
        workspace, _dataset, revision, _validation = required_values_workspace(self)
        context = self.app.state.context
        now = datetime.now(UTC)
        for error, expected in (
            (ConnectorTransportError("private-url"), "connection to Odoo failed"),
            (
                ConnectorAuthenticationError("private-key"),
                "did not accept the read key",
            ),
            (ConnectorAuthorizationError("private-principal"), "read key cannot read"),
        ):
            with self.subTest(error=type(error)):
                attempt = MatchingOrderCheckAttempt(
                    check_id=str(uuid4()),
                    workspace_id=workspace,
                    status=MatchingOrderCheckStatus.FAILED,
                    phase=MatchingOrderCheckPhase.COMPLETE,
                    message="Odoo check could not be completed",
                    progress_percent=100,
                    created_at=now,
                    updated_at=now,
                    finished_at=now,
                    failure_message=_safe_matching_order_failure(error),
                )
                with patch.object(
                    context.matching_order, "check_attempt", return_value=attempt
                ):
                    page = self.client.get(
                        f"/workspaces/{workspace}/mapping?order_check={attempt.check_id}"
                    )
                self.assertEqual(page.status_code, 200, page.text)
                health = page.text.split('aria-labelledby="identity-health-title"', 1)[
                    1
                ].split("</section>", 1)[0]
                self.assertIn('role="alert"', health)
                self.assertIn(expected, health)
                self.assertIn("Check saved identities again", health)
                self.assertIn('id="mapping-identity-check"', health)
                self.assertNotIn("private-", health)
                self.assertEqual(
                    context.queries.get_mapping_revision(workspace),
                    revision,
                )


if __name__ == "__main__":
    unittest.main()
