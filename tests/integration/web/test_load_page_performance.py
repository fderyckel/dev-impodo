"""Admit complete load pages once and keep progress reads off the event loop."""

import asyncio
from datetime import UTC, datetime
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.application.workspace.access import current_workspace_access_context
from impodo.application.workspace.execution.job_models import (
    LoadJob,
    LoadJobStatus,
    LoadPhase,
)
from impodo.domain.shared.access import AuthorizationError, Capability
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_execution import compared_hierarchy, json2_adapters
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture

LOAD_PAGES = ("load", "load/review", "load/confirm", "load/outcome")


class LoadPagePerformanceTests(HierarchyWorkflowFixture):
    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        workspace_id, _, (target, snapshot, identities) = compared_hierarchy(
            self,
            major=major,
            mode=mode,
        )
        prefix = f"/workspaces/{workspace_id}/load"
        requests_before = len(target.requests)
        # Remote pages retain Test/Production credential-owner lookup pairs:
        # landing needs one pair, review three, and confirmation two. Each
        # pair adds two statements; both modes keep the same owner ceiling.
        for suffix, status, marker, local_sql, remote_sql in (
            ("", 303, prefix + "/review", 23, 25),
            ("/review", 200, "Check what will change in Odoo", 79, 85),
            ("/confirm", 200, "Confirm and load", 123, 127),
            ("/outcome", 303, prefix + "/review", 87, 87),
        ):
            with self.subTest(suffix=suffix):
                response = self.bounded_page(
                    prefix + suffix,
                    sql_limit=remote_sql if mode == "REMOTE" else local_sql,
                )
                self.assertEqual(response.status_code, status, response.text)
                if status == 303:
                    self.assertEqual(response.headers["location"], marker)
                else:
                    self.assertIn(marker, response.text)
        self.assertEqual(len(target.requests), requests_before)
        self.assertEqual(target.writes, [])

        writer, _ = json2_adapters(self, workspace_id, target)
        run = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=self.actor,
            **identities,
        )
        self.assertEqual(run.status.value, "COMPLETED")
        writes_before = list(target.writes)
        requests_before = len(target.requests)
        outcome = self.bounded_page(prefix + "/outcome")
        self.assertEqual(outcome.status_code, 200, outcome.text)
        self.assertIn("Verify result", outcome.text)
        self.assertEqual(target.writes, writes_before)
        self.assertEqual(len(target.requests), requests_before)
        landing = self.bounded_page(prefix + "?rows_page=2")
        self.assertEqual(landing.status_code, 303)
        self.assertEqual(
            landing.headers["location"],
            prefix + "/outcome?rows_page=2",
        )

    def test_closed_workspace_keeps_verified_load_outcome_inspectable(self):
        workspace_id, _, (target, snapshot, identities) = compared_hierarchy(self)
        writer, reader = json2_adapters(self, workspace_id, target)
        run = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=self.actor,
            **identities,
        )
        verification = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=self.actor,
            **{
                key: value
                for key, value in identities.items()
                if key.startswith("write_")
            },
        )
        self.assertEqual(verification.status.value, "VERIFIED")
        workspace = self.context.migration_workspaces.get(
            workspace_id, actor=self.actor
        )
        closed = self.context.migration_workspaces.close(
            workspace_id,
            actor=self.actor,
            expected_revision=workspace.optimistic_revision,
        )
        self.assertEqual(closed.state.value, "CLOSED")
        writes_before = list(target.writes)
        requests_before = len(target.requests)

        page = self.bounded_page(f"/workspaces/{workspace_id}/load/outcome")

        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn(f"{verification.verified_count} verified in Odoo", page.text)
        self.assertEqual(target.writes, writes_before)
        self.assertEqual(len(target.requests), requests_before)
        self.assertEqual(
            self.context.reconciliation.current(workspace_id).semantic_hash,
            verification.semantic_hash,
        )

    def bounded_page(self, url, *, sql_limit=200):
        with (
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(url, follow_redirects=False)
        # Admission and projection share the registry, linkage, engine and
        # Data-version owners. Their three store validators run once each.
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 3)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), sql_limit)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        return response

    def test_denied_load_pages_do_not_read_jobs_or_child_evidence(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        with (
            patch.object(
                self.context.workspace_access,
                "resolve",
                side_effect=AuthorizationError("denied"),
            ),
            patch.object(
                self.context.load_jobs,
                "active",
                side_effect=AssertionError("job read before admission"),
            ),
            patch.object(
                self.context.navigation,
                "get_for_workspace",
                side_effect=AssertionError("navigation read before admission"),
            ),
            patch.object(
                self.context.execution,
                "current_preview",
                side_effect=AssertionError("preview read before admission"),
            ),
            patch.object(
                self.context.execution,
                "current_review",
                side_effect=AssertionError("review read before admission"),
            ),
        ):
            for area in LOAD_PAGES:
                with (
                    self.subTest(area=area),
                    collect_duckdb_request_timings() as timings,
                ):
                    response = self.client.get(
                        f"/workspaces/{workspace_id}/{area}",
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.text, "Workspace not found")
                    self.assertEqual(timings.connection_count, 0)

    def test_active_jobs_are_read_off_loop_after_admission(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)

        def active(requested_id):
            self.assertEqual(requested_id, workspace_id)
            self.assertEqual(
                current_workspace_access_context().workspace_id,
                workspace_id,
            )
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return SimpleNamespace(job_id="already-running")

        with (
            patch.object(self.context.load_jobs, "active", side_effect=active),
            patch.object(
                self.context.navigation,
                "get_for_workspace",
                side_effect=AssertionError("projection after active job"),
            ),
            patch.object(
                self.context.execution,
                "current_preview",
                side_effect=AssertionError("preview after active job"),
            ),
        ):
            for area in LOAD_PAGES:
                with self.subTest(area=area):
                    response = self.client.get(
                        f"/workspaces/{workspace_id}/{area}",
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(
                        response.headers["location"],
                        f"/workspaces/{workspace_id}/load/progress/already-running",
                    )

    def test_failed_render_closes_owners_and_next_page_revalidates(self):
        workspace_id, _, _ = compared_hierarchy(self)
        real_connect = DuckDbConnectionFactory._connect_with_lock_wait
        owners, threads = [], set()

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

        url = f"/workspaces/{workspace_id}/load/review"
        with patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect):
            with (
                patch(
                    "impodo.web.routers.execution._render",
                    side_effect=ValueError("render failed"),
                ),
                self.assertRaisesRegex(ValueError, "render failed"),
            ):
                self.client.get(url)
            assert_closed()
            self.assertEqual(self.bounded_page(url).status_code, 200)
            assert_closed()

    def test_progress_html_reads_stores_off_loop_and_status_stays_in_memory(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        access = self.context.workspace_access.resolve(
            workspace_id,
            actor=self.actor,
            capability=Capability.PROJECT_VIEW,
        )
        now = datetime.now(UTC)
        job = LoadJob(
            job_id="progress-fixture",
            access_context=access,
            workspace_id=workspace_id,
            migration_project_name="Fictional load",
            target_database="fictional",
            target_server="fictional.example.test",
            target_environment="Test",
            status=LoadJobStatus.RUNNING,
            phase=LoadPhase.WRITING,
            message="Sending records to Odoo",
            total_rows=10,
            completed_rows=4,
            created_count=4,
            updated_count=0,
            attention_count=0,
            relationship_pending_count=0,
            relationship_total_count=0,
            load_group_number=1,
            load_group_count=1,
            progress_percent=40,
            execution_run_id="",
            verification_complete=False,
            completion_warning="",
            completion_warning_code="",
            created_at=now,
            started_at=now,
            updated_at=now,
            finished_at=None,
            failure_message="",
        )
        workspace_reader = self.context.migration_workspaces.get
        data_reader = self.context.data_versions.get

        def read_workspace(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return workspace_reader(*args, **kwargs)

        def read_data(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return data_reader(*args, **kwargs)

        prefix = f"/workspaces/{workspace_id}/load/progress/{job.job_id}"
        with (
            patch.object(self.context.load_jobs, "get", return_value=job),
            patch.object(
                self.context.migration_workspaces, "get", side_effect=read_workspace
            ),
            patch.object(self.context.data_versions, "get", side_effect=read_data),
            collect_duckdb_request_timings() as timings,
        ):
            page = self.client.get(prefix)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("4 of 10 records have a final write result", page.text)
        # Trusted job admission does not need the workspace engine. The HTML
        # context retains its registry, linkage and Data-version owners once.
        self.assertLessEqual(timings.connection_count, 3)
        with (
            patch.object(self.context.load_jobs, "get", return_value=job),
            patch.object(
                self.context.migration_workspaces,
                "get",
                side_effect=AssertionError("status reopened workspace"),
            ),
            patch.object(
                self.context.data_versions,
                "get",
                side_effect=AssertionError("status reopened Data version"),
            ),
            collect_duckdb_request_timings() as timings,
        ):
            status = self.client.get(prefix + "/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["completed_rows"], 4)
        self.assertEqual(status.json()["progress_percent"], 40)
        self.assertEqual(timings.connection_count, 0)
