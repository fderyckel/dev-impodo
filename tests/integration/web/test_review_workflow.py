"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

import time
from threading import Event, get_ident

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.preflight_jobs import PreflightJobManager
from impodo.web.composition.page_reads import (
    run_local_operation as run_bounded_local_operation,
)
from impodo.web.routers import preflight as preflight_router
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ConnectorAuthenticationError,
    ConnectorTransportError,
    OdooReadFailureCode,
    OdooReadWorkflowError,
    PreflightRequirementPlan,
    ProjectSetupBrowserTestCase,
    SecretStoreError,
    SimpleNamespace,
    patch,
)
from tests.support.identity import resolve_local_actor


class ReviewWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_background_comparison_returns_progress_without_duplicate_work(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state, _schema = self._frozen_odoo_source_workspace()
        manager = PreflightJobManager()
        context.preflight_jobs = manager
        started = Event()
        release = Event()
        event_loop_threads = []
        callback_threads = []
        operation_id = "70000000-0000-4000-8000-000000000001"

        async def observe_start_callback(function):
            event_loop_threads.append(get_ident())

            def invoke():
                callback_threads.append(get_ident())
                return function()

            return await run_bounded_local_operation(invoke)

        def compare(
            _workspace_id,
            *,
            reader,
            actor,
            progress,
            local_operation,
            operation_id,
            comparison_request,
            current_read_credential_binding_hash,
        ):
            self.assertEqual(actor, resolve_local_actor(context))
            self.assertIsNotNone(reader)
            self.assertIsNotNone(local_operation)
            self.assertTrue(operation_id)
            self.assertEqual(comparison_request.operation_id, operation_id)
            self.assertIsNotNone(current_read_credential_binding_hash)
            progress("READING")
            started.set()
            release.wait(timeout=30)
            return SimpleNamespace(
                run_id="50000000-0000-4000-8000-000000000001",
                attention_count=0,
            )

        try:
            with (
                patch.object(context.preflight, "compare", side_effect=compare),
                patch.object(
                    context.preflight,
                    "comparison_input_hash",
                    return_value="sha256:" + "1" * 64,
                ),
                patch.object(
                    context.execution,
                    "current_preview",
                    return_value=None,
                ) as current_preview,
                patch(
                    "impodo.web.routers.preflight._rebind_remote_read_access",
                    return_value=None,
                ),
                patch.object(
                    preflight_router,
                    "run_local_operation",
                    side_effect=observe_start_callback,
                ),
            ):
                with collect_duckdb_request_timings() as start_timings:
                    started_response = self.client.post(
                        f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                        data={
                            "csrf_token": self.csrf,
                            "operation_id": operation_id,
                            "read_api_key": "replacement-read-key",
                            "read_api_key_storage": "session",
                        },
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                self.assertEqual(started_response.status_code, 303)
                self.assertIn("/preflight/", started_response.headers["location"])
                self.assertTrue(started.wait(timeout=10))
                self.assertEqual(len(event_loop_threads), 1)
                self.assertEqual(len(callback_threads), 1)
                self.assertNotEqual(event_loop_threads, callback_threads)
                self.assertGreater(start_timings.connection_count, 0)
                # The request middleware and callback touch the registry,
                # Workspace reference, Workspace engine, and protected secret
                # owners. Retention keeps repeated repository calls within
                # those owners below this route-specific physical ceiling.
                self.assertLessEqual(start_timings.connection_count, 6)
                self.assertLessEqual(start_timings.schema_check_count, 3)

                progress_page = self.client.get(started_response.headers["location"])
                repeated = self.client.post(
                    f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                    data={
                        "csrf_token": self.csrf,
                        "operation_id": operation_id,
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(progress_page.status_code, 200)
                self.assertIn("data-preflight-job", progress_page.text)
                self.assertIn('aria-label="Impodo workflow"', progress_page.text)
                self.assertIn("Current data project", progress_page.text)
                self.assertIn("Stage 5: Final review", progress_page.text)
                self.assertIn("Comparison progress", progress_page.text)
                self.assertEqual(
                    repeated.headers["location"],
                    started_response.headers["location"],
                )
                release.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = self.client.get(
                        f"{started_response.headers['location']}/status"
                    ).json()
                    if status["status"] == "SUCCEEDED":
                        break
                    time.sleep(0.005)
                else:
                    self.fail("background comparison did not finish")
                self.assertEqual(
                    status["redirect_url"],
                    f"/workspaces/{workspace_state.workspace_id}/load/review",
                )
                current_preview.assert_not_called()
                missing_id = "60000000-0000-4000-8000-000000000001"
                interrupted_page = self.client.get(
                    f"/workspaces/{workspace_state.workspace_id}/preflight/"
                    f"{missing_id}",
                    follow_redirects=False,
                )
                interrupted_status = self.client.get(
                    f"/workspaces/{workspace_state.workspace_id}/preflight/"
                    f"{missing_id}/status"
                )
                self.assertEqual(interrupted_page.status_code, 303)
                self.assertEqual(interrupted_status.status_code, 410)
                self.assertEqual(
                    interrupted_status.json()["redirect_url"],
                    f"/workspaces/{workspace_state.workspace_id}/summary",
                )
        finally:
            release.set()
            manager.shutdown()
            context.preflight_jobs = None

    def test_background_comparison_opens_blocked_rows_before_load(self) -> None:
        context = self.app.state.context
        workspace_state, _schema = self._frozen_odoo_source_workspace()
        manager = PreflightJobManager()
        context.preflight_jobs = manager

        def compare(
            _workspace_id,
            *,
            reader,
            actor,
            progress,
            local_operation,
            operation_id,
            comparison_request,
            current_read_credential_binding_hash,
        ):
            self.assertIsNotNone(local_operation)
            self.assertTrue(operation_id)
            return SimpleNamespace(
                run_id="50000000-0000-4000-8000-000000000002",
                attention_count=36,
            )

        try:
            with (
                patch.object(context.preflight, "compare", side_effect=compare),
                patch.object(
                    context.preflight,
                    "comparison_input_hash",
                    return_value="sha256:" + "2" * 64,
                ),
                patch(
                    "impodo.web.routers.preflight._rebind_remote_read_access",
                    return_value=None,
                ),
            ):
                started = self.client.post(
                    f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                    data={
                        "csrf_token": self.csrf,
                        "read_api_key": "replacement-read-key",
                        "read_api_key_storage": "session",
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(started.status_code, 303)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = self.client.get(
                        f"{started.headers['location']}/status"
                    ).json()
                    if status["status"] == "SUCCEEDED":
                        break
                    time.sleep(0.005)
                else:
                    self.fail("background comparison did not finish")
                self.assertEqual(
                    status["redirect_url"],
                    f"/workspaces/{workspace_state.workspace_id}/summary"
                    "?status=attention#readiness-rows",
                )
                self.assertIn("36 records needing attention", status["completion_message"])
        finally:
            manager.shutdown()
            context.preflight_jobs = None

    def test_direct_comparison_opens_blocked_rows_before_load(self) -> None:
        context = self.app.state.context
        workspace_state, _schema = self._frozen_odoo_source_workspace()
        context.preflight_jobs = None
        with (
            patch.object(
                context.preflight,
                "compare",
                return_value=SimpleNamespace(attention_count=36),
            ),
            patch(
                "impodo.web.routers.preflight._rebind_remote_read_access",
                return_value=None,
            ),
        ):
            response = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={
                    "csrf_token": self.csrf,
                    "read_api_key": "replacement-read-key",
                    "read_api_key_storage": "session",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/summary"
            "?status=attention#readiness-rows",
        )

    def test_summary_reconnects_missing_remote_key_without_losing_schema(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state, schema = self._frozen_odoo_source_workspace()

        def compare_with_reader(
            _project_id,
            *,
            reader,
            actor,
            local_operation,
            operation_id,
        ):
            self.assertEqual(actor, resolve_local_actor(context))
            self.assertIsNotNone(local_operation)
            self.assertTrue(operation_id)
            return reader(
                PreflightRequirementPlan((), (), (), source_record_count=0)
            )

        with patch.object(
            context.preflight,
            "compare",
            side_effect=compare_with_reader,
        ):
            blocked = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
            )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn("Enter the Odoo read key", blocked.text)
        self.assertIn('name="read_api_key"', blocked.text)
        self.assertIn('name="read_api_key_storage"', blocked.text)
        self.assertIn("Save key and compare", blocked.text)
        self.assertIn("Nothing was changed in Odoo", blocked.text)
        self.assertNotIn("We could not complete that action", blocked.text)
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.read_identity_calls, [])

        with patch.object(context.preflight, "compare") as compare:
            reconnected = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={
                    "csrf_token": self.csrf,
                    "read_api_key": "replacement-read-key",
                    "read_api_key_storage": "session",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )

        self.assertEqual(reconnected.status_code, 303, reconnected.text)
        compare.assert_called_once()
        self.assertEqual(
            self.schema_calls,
            [(workspace_state.workspace_id, "replacement-read-key")],
        )
        self.assertEqual(
            self.read_identity_calls,
            [
                (
                    workspace_state.workspace_id,
                    "replacement-read-key",
                    ("res.partner",),
                )
            ],
        )
        rebound = context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
        self.assertEqual(rebound.content_hash, schema.content_hash)
        self.assertNotEqual(
            rebound.read_credential_binding_hash,
            schema.read_credential_binding_hash,
        )
        page = self.client.get(reconnected.headers["location"])
        self.assertNotIn("replacement-read-key", page.text)
        self.assertNotIn('id="comparison-recovery"', page.text)

    def test_remote_reference_failure_returns_to_matching_without_key_form(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state, schema = self._frozen_odoo_source_workspace()
        available_key = SimpleNamespace(
            available=True,
            binding_hash=schema.read_credential_binding_hash,
        )

        with (
            patch.object(
                context.preflight,
                "compare",
                side_effect=OdooReadWorkflowError(
                    OdooReadFailureCode.REFERENCE_POLICY_MISMATCH,
                    "Internal wording must not select or leak into recovery",
                    support_reference="res.partner.country_id -> res.country",
                ),
            ),
            patch(
                "impodo.web.presenters.summary.get_target_credential_status",
                return_value=available_key,
            ),
        ):
            blocked = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
            )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn('id="comparison-recovery"', blocked.text)
        self.assertIn("Review the linked field match", blocked.text)
        self.assertIn(
            f'href="/workspaces/{workspace_state.workspace_id}/mapping"',
            blocked.text,
        )
        self.assertIn('data-read-credential-dialog', blocked.text)
        self.assertIn('data-auto-open="false"', blocked.text)
        self.assertNotIn("Internal wording", blocked.text)

    def test_remote_compare_recovery_matches_the_classified_failure(self) -> None:
        context = self.app.state.context
        workspace_state, schema = self._frozen_odoo_source_workspace()
        available_key = SimpleNamespace(
            available=True,
            binding_hash=schema.read_credential_binding_hash,
        )
        cases = (
            (
                ConnectorAuthenticationError("raw rejected key response"),
                "Replace the Odoo read key",
                'name="read_api_key"',
            ),
            (
                ConnectorTransportError("HTTP 503 raw upstream response"),
                "Try the comparison again",
                f'action="/workspaces/{workspace_state.workspace_id}/summary/compare"',
            ),
            (
                OdooReadWorkflowError(
                    OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
                    "raw schema mismatch",
                ),
                "Refresh the Odoo data structure",
                f'href="/workspaces/{workspace_state.workspace_id}/schema"',
            ),
            (
                OdooReadWorkflowError(
                    OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                    "raw prepared mismatch",
                ),
                "Prepare and approve the data again",
                f'href="/workspaces/{workspace_state.workspace_id}/prepare"',
            ),
            (
                SecretStoreError("raw Windows credential-store failure"),
                "Impodo could not save the comparison",
                "COMPARISON_STORAGE_FAILED",
            ),
        )

        for failure, title, expected_action in cases:
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch.object(
                        context.preflight,
                        "compare",
                        side_effect=failure,
                    ),
                    patch(
                        "impodo.web.presenters.summary.get_target_credential_status",
                        return_value=available_key,
                    ),
                ):
                    blocked = self.client.post(
                        f"/workspaces/{workspace_state.workspace_id}/summary/compare",
                        data={"csrf_token": self.csrf},
                        headers=POST_HEADERS,
                    )

                self.assertEqual(blocked.status_code, 422)
                self.assertIn(title, blocked.text)
                self.assertIn(expected_action, blocked.text)
                if not isinstance(failure, ConnectorAuthenticationError):
                    self.assertIn('data-auto-open="false"', blocked.text)
                self.assertNotIn("raw ", blocked.text)
