"""Admit complete saved pages before child reads and release owners on exit."""

import asyncio
from dataclasses import replace
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import duckdb
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.application.workspace.access import current_workspace_access_context
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.web.security import WorkspaceAccessMiddleware
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class WorkspacePageAdmissionTests(ProjectSetupBrowserTestCase):
    def test_successful_page_cannot_bypass_admission(self):
        app = FastAPI()
        access = Mock()
        app.add_middleware(
            WorkspaceAccessMiddleware,
            access=access,
            identity_resolver=self.app.state.context.identity_resolver,
        )
        app.add_middleware(SessionMiddleware, secret_key="test-only-session")

        @app.get("/launch")
        async def launch(request: Request):
            request.session["authenticated"] = True
            return {"status": "ok"}

        @app.get("/workspaces/{workspace_id}/schema")
        async def bypass(request: Request, workspace_id: str):
            return HTMLResponse("Unverified page")

        with TestClient(app) as client:
            client.get("/launch")
            with self.assertRaisesRegex(
                RuntimeError, "did not perform its access check"
            ):
                client.get(f"/workspaces/{uuid4()}/schema")
        access.resolve_visible_workspace_access.assert_not_called()

    def test_active_jobs_redirect_after_admission_before_page_projection(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context

        def active(requested_id):
            self.assertEqual(requested_id, workspace_id)
            self.assertEqual(
                current_workspace_access_context().workspace_id, workspace_id
            )
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return SimpleNamespace(job_id="active-preparation")

        with (
            patch.object(context.preparation_jobs, "active", side_effect=active),
            patch(
                "impodo.web.routers.mapping._render_mapping",
                side_effect=AssertionError("editor read"),
            ),
            patch(
                "impodo.web.routers.summary._render_summary",
                side_effect=AssertionError("summary read"),
            ),
        ):
            for area in ("mapping", "summary"):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/{area}", follow_redirects=False
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(
                    response.headers["location"],
                    f"/workspaces/{workspace_id}/preparation/active-preparation",
                )

    def test_denied_pages_do_not_read_child_data_or_active_jobs(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context
        original = context.queries.get_mapping_working_draft(workspace_id)
        with (
            patch.object(
                context.workspace_access,
                "resolve",
                side_effect=AuthorizationError("denied"),
            ),
            patch.object(
                context.preparation_jobs,
                "active",
                side_effect=AssertionError("job state leaked"),
            ),
            patch.object(
                context,
                "preflight_jobs",
                SimpleNamespace(
                    active=Mock(side_effect=AssertionError("job state leaked"))
                ),
            ),
            patch.object(
                context.queries,
                "get_source_page",
                side_effect=AssertionError("child read"),
            ),
            patch.object(
                context.navigation,
                "get_source_readiness",
                side_effect=AssertionError("source read"),
            ),
            patch(
                "impodo.web.routers.summary._render_summary",
                side_effect=AssertionError("summary read"),
            ),
        ):
            for area in ("files", "sources", "schema", "mapping", "summary"):
                with (
                    self.subTest(area=area),
                    collect_duckdb_request_timings() as timings,
                ):
                    response = self.client.get(f"/workspaces/{workspace_id}/{area}")
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.text, "Workspace not found")
                    self.assertEqual(timings.connection_count, 0)
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id), original
        )

    def test_page_owners_close_on_success_failure_and_the_next_request(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        real_connect = DuckDbConnectionFactory._connect_with_lock_wait
        owners = []
        threads = set()

        def connect(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            threads.add(get_ident())
            owner = real_connect(factory, path, config)
            owners.append(owner)
            return owner

        def assert_closed():
            self.assertTrue(owners)
            self.assertEqual(len(threads), 1)
            for owner in owners:
                with self.assertRaises(duckdb.ConnectionException):
                    owner.execute("SELECT 1")
            owners.clear()
            threads.clear()

        with patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect):
            for area in ("schema", "mapping", "summary"):
                with (
                    self.subTest(area=area),
                    collect_duckdb_request_timings() as timings,
                ):
                    response = self.client.get(f"/workspaces/{workspace_id}/{area}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(timings.connection_count, 4)
                    self.assertEqual(timings.schema_check_count, 3)
                assert_closed()
            with (
                patch(
                    "impodo.web.routers.mapping._render_mapping",
                    side_effect=ValueError("render failed"),
                ),
                self.assertRaisesRegex(ValueError, "render failed"),
            ):
                self.client.get(f"/workspaces/{workspace_id}/mapping")
            assert_closed()
            with collect_duckdb_request_timings() as timings:
                self.assertEqual(
                    self.client.get(f"/workspaces/{workspace_id}/mapping").status_code,
                    200,
                )
            self.assertEqual(timings.connection_count, 4)
            self.assertEqual(timings.schema_check_count, 3)
            assert_closed()

    def test_recipe_journeys_and_closed_workspace_policy_precede_page_reads(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        access = context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.PROJECT_VIEW
        )
        with (
            patch.object(
                context.workspace_access,
                "resolve",
                return_value=replace(access, run_purpose="TEST"),
            ),
            patch.object(
                context.navigation,
                "get_source_readiness",
                side_effect=AssertionError("source read"),
            ),
            patch(
                "impodo.web.routers.summary._render_summary",
                side_effect=AssertionError("summary read"),
            ),
        ):
            for area in ("mapping", "summary"):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/{area}", follow_redirects=False
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(
                    response.headers["location"],
                    f"/projects/{access.project_id}/test-runs/{access.migration_run_id}/fresh-data",
                )
        with (
            patch.object(
                context.workspace_access,
                "resolve",
                return_value=replace(
                    access, run_purpose="TEST", recipe_application_id=str(uuid4())
                ),
            ),
            patch.object(
                context.queries,
                "get_source_page",
                side_effect=AssertionError("schema read"),
            ),
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/schema", follow_redirects=False
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                f"/projects/{access.project_id}/runs/{access.migration_run_id}",
            )
        workspace = context.migration_workspaces.get(workspace_id, actor=actor)
        context.migration_workspaces.close(
            workspace_id, actor=actor, expected_revision=workspace.optimistic_revision
        )
        for area in ("files", "sources", "schema", "mapping", "summary"):
            response = self.client.get(
                f"/workspaces/{workspace_id}/{area}", follow_redirects=False
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"], f"/projects/{access.project_id}"
            )
