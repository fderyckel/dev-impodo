"""Bound load submission reads and release local owners before dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class LoadSubmissionPerformanceTests(HierarchyWorkflowFixture):
    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        target = HierarchyOdoo(schema)
        report = self.context.preflight.compare(
            workspace_id,
            reader=target.compare,
            actor=self.actor,
        )
        self.assertEqual(report.status, "READY")
        preview = run_local_operation_sync(
            self.context.execution.current_preview,
            workspace_id,
            actor=self.actor,
        )
        self.assertIsNotNone(preview)
        self.assertTrue(preview.can_load, preview.scope_error)
        state = self.context.queries.get(workspace_id)
        owner = self.context.target_credential_workspace(
            workspace_id,
            workspace_state=state,
            actor=self.actor,
        )
        store_target_credential(
            self.context.secret_store,
            owner,
            TargetCredentialRole.WRITE,
            "fictional-write-key",
            persistent=False,
        )
        form = {
            "csrf_token": self.csrf,
            "snapshot_hash": preview.snapshot.semantic_hash,
            "intent_assessment_hash": preview.business_intent_assessment_hash,
            "batch_rows": "10",
        }
        requests_before = len(target.requests)
        preview_reader = self.context.execution.current_preview
        cutover_reader = self.context.cutover_plans.assert_application_can_execute

        def bounded_preview(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            return preview_reader(*args, **kwargs)

        def bounded_authorization(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            return cutover_reader(*args, **kwargs)

        def enqueue(*args, **kwargs):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            self.assertEqual(args[0], workspace_id)
            self.assertEqual(kwargs["access_context"].workspace_id, workspace_id)
            self.assertEqual(kwargs["total_rows"], preview.snapshot.write_count)
            self.assertEqual(
                kwargs["target_database"], preview.snapshot.target_database
            )
            self.assertIsNotNone(kwargs["work"])
            return SimpleNamespace(job_id="isolated-load")

        with (
            patch.object(
                self.context.execution,
                "current_preview",
                side_effect=bounded_preview,
            ),
            patch.object(
                type(self.context.cutover_plans),
                "assert_application_can_execute",
                side_effect=bounded_authorization,
            ),
            patch.object(
                self.context.load_jobs, "enqueue", side_effect=enqueue
            ) as dispatch,
        ):
            # Each local phase has its own bounded registry/linkage, engine,
            # and Data-version owners. Dispatch must add no retained scope.
            queued = self.bounded(
                lambda: self._post(f"/workspaces/{workspace_id}/load", form),
                19,
                13,
                160,
            )
            self.assertEqual(queued.status_code, 303, queued.text)
            self.assertTrue(
                queued.headers["location"].endswith("/load/progress/isolated-load")
            )
            dispatch.assert_called_once()

            dispatch.reset_mock()
            rejected = self.bounded(
                lambda: self._post(
                    f"/workspaces/{workspace_id}/load",
                    {**form, "snapshot_hash": "sha256:" + "0" * 64},
                ),
                17,
                11,
                250,
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertIn("The load preview changed", rejected.text)
            dispatch.assert_not_called()

        self.assertEqual(len(target.requests), requests_before)
        self.assertEqual(target.writes, [])
        self.assertEqual(self.context.preflight.current_report(workspace_id), report)
        self.assertEqual(
            self.context.execution.current_preview(workspace_id, actor=self.actor),
            preview,
        )

        with (
            patch.object(
                self.context.load_jobs,
                "active",
                return_value=SimpleNamespace(job_id="already-running"),
            ),
            patch.object(
                type(self.context), "target_credential_workspace"
            ) as credential_reader,
            patch.object(self.context.load_jobs, "enqueue") as dispatch,
        ):
            active = self._post(f"/workspaces/{workspace_id}/load", form)
        self.assertEqual(active.status_code, 303)
        self.assertTrue(
            active.headers["location"].endswith("/load/progress/already-running")
        )
        credential_reader.assert_not_called()
        dispatch.assert_not_called()

        intent_reader = self.context.execution.require_business_intent
        intent_checks = 0

        def reject_changed_intent(*args, **kwargs):
            nonlocal intent_checks
            intent_checks += 1
            if intent_checks == 2:
                raise WorkspaceError("Business expectations changed")
            return intent_reader(*args, **kwargs)

        with (
            patch.object(
                self.context.execution,
                "require_business_intent",
                side_effect=reject_changed_intent,
            ),
            patch.object(self.context.load_jobs, "enqueue") as dispatch,
        ):
            invalid_intent = self._post(f"/workspaces/{workspace_id}/load", form)
        self.assertEqual(invalid_intent.status_code, 422, invalid_intent.text)
        self.assertIn("Business expectations changed", invalid_intent.text)
        dispatch.assert_not_called()
        self.assertEqual(intent_checks, 2)

    def bounded(self, callback, opens, checks, sql_limit):
        with (
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = callback()
        self.assertLessEqual(timings.connection_count, opens)
        self.assertLessEqual(timings.schema_check_count, checks)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), sql_limit)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        return response
