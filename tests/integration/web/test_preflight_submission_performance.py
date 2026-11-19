"""Bound browser comparison submission and durable job transitions."""

import asyncio
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory, _read_databases
from impodo.application.preflight_jobs import PreflightJobStatus
from impodo.domain.project.foundation import MigrationOperationState
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class PreflightSubmissionPerformanceTests(HierarchyWorkflowFixture):
    def test_odoo_19_and_20_local_and_remote(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self.exercise(major, mode)

    def exercise(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        self.reopen(preflight_jobs_enabled=True)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        target = HierarchyOdoo(schema)
        manager = self.context.preflight_jobs
        operation_id = str(uuid4())
        release = Event()
        done = Event()
        worker_ready = Event()
        run_work = manager._run_work

        def controlled_work(*args):
            worker_ready.set()
            try:
                if release.wait(30):
                    run_work(*args)
            finally:
                done.set()

        def read_target(_context, _state, requirements, **_kwargs):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            return target.compare(requirements)

        url = f"/workspaces/{workspace_id}/summary/compare"
        form = {"csrf_token": self.csrf, "operation_id": operation_id}
        try:
            with (
                patch.object(manager, "_run_work", side_effect=controlled_work),
                patch(
                    "impodo.web.routers.preflight._read_readiness_snapshots",
                    side_effect=read_target,
                ) as target_reader,
            ):
                started = self.submit_comparison(url, form, opens=7, schema_checks=4, sql_limit=73)
                self.assertEqual(started.status_code, 303, started.text)
                self.assertTrue(started.headers["location"].endswith(operation_id))
                self.assertTrue(worker_ready.wait(10))
                self.assertEqual(
                    manager._attempt_store.get_preflight_comparison(operation_id, workspace_id).stage,
                    "QUEUED",
                )
                replay = self.submit_comparison(url, form, opens=6, schema_checks=4, sql_limit=70)
                self.assertEqual(replay.status_code, 303, replay.text)
                self.assertEqual(replay.headers["location"], started.headers["location"])
                conflict = self.submit_comparison(
                    url, {**form, "operation_id": str(uuid4())},
                    opens=10, schema_checks=7, sql_limit=165,
                )
                self.assertEqual(conflict.status_code, 422, conflict.text)
                self.assertEqual(target.requests, [])
                release.set()
                self.assertTrue(done.wait(30))
                finished = manager.get(workspace_id, operation_id)
                self.assertIs(finished.status, PreflightJobStatus.SUCCEEDED, finished.failure)
                saved = manager._attempt_store.get_preflight_comparison(operation_id, workspace_id)
                self.assertIs(saved.state, MigrationOperationState.COMMITTED)
                self.assertEqual(saved.result["preflight_run_id"], finished.preflight_run_id)
                self.assertEqual(self.context.preflight.current_report(workspace_id).operation_id, operation_id)
                target_reader.assert_called_once()
                self.assertEqual(target.writes, [])
        finally:
            release.set()
            manager.shutdown()

    def submit_comparison(self, url, form, *, opens, schema_checks, sql_limit):
        opened = []
        connect = DuckDbConnectionFactory._connect_with_lock_wait

        def observe(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            opened.append(Path(path).name)
            return connect(factory, path, config)

        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
            patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", observe),
        ):
            response = self._post(url, form)
        # Admission opens registry and workspace linkage. The submission uses
        # those two owners plus Data version and engine. First dispatch needs
        # one independent registry connection after these owners close.
        self.assertLessEqual(timings.connection_count, opens, opened)
        self.assertLessEqual(timings.schema_check_count, schema_checks)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), sql_limit)
        return response
