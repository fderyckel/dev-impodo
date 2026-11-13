"""Enter preparation only from a verified current authoring result."""

from __future__ import annotations

import json
import unittest
from asyncio import get_running_loop
from dataclasses import replace
from pathlib import Path
from threading import get_ident
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.web.composition.local_identity import LocalRequestIdentityResolver
from tests.performance import test_preparation_scale as scale
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _wait_for_preparation,
)
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class AuthoringPreparationRecoveryBrowserTests(unittest.TestCase):
    def _prepared_workspace(self, major):
        fixture = ProjectSetupBrowserTestCase(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.root = Path(fixture.temporary.name)
        fixture.context = fixture.app.state.context
        fixture.artifacts = fixture.context.artifacts
        self.addCleanup(fixture.context.preparation_jobs.shutdown)

        def schema(**values):
            return OdooSchemaCatalog(**{**values, "odoo_version": f"{major}.0"})

        with patch.object(scale, "OdooSchemaCatalog", side_effect=schema):
            workspace_id, _, _ = (
                scale.PreparationWorkflowScaleTests._prepare_project_and_evidence(
                    fixture,
                    row_count=5,
                    column_count=5,
                    mapped_field_count=5,
                )
            )
        context = fixture.context
        actor = resolve_local_actor(context)
        context.preparation.prepare(workspace_id, actor=actor)
        revision = context.queries.get_mapping_revision(workspace_id)
        assert revision is not None
        result = context.preparation_recovery.current(
            workspace_id, revision.definition.content_hash, actor=actor
        )
        self.assertIsNotNone(result)
        assert result is not None
        return fixture, workspace_id, result

    def test_verified_results_resume_and_retired_results_prepare_again(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, workspace_id, original = self._prepared_workspace(major)
                context, client = fixture.context, fixture.client
                actor = resolve_local_actor(context)
                prepare_url = f"/workspaces/{workspace_id}/prepare"
                current_page = client.get(prepare_url, follow_redirects=False)
                self.assertEqual(current_page.status_code, 303, current_page.text)
                self.assertEqual(
                    current_page.headers["location"],
                    f"/workspaces/{workspace_id}/normalization",
                )

                # Both review destinations come from the verified result,
                # without consulting independently stale review summaries.
                with (
                    patch.object(
                        context.preparation_recovery,
                        "current_for_review",
                        return_value=replace(
                            original, status=PreparationJobStatus.REVIEW_REQUIRED
                        ),
                    ) as recover,
                    patch.object(
                        context.resolution,
                        "current_summary",
                        side_effect=AssertionError("A summary cannot admit recovery"),
                    ),
                    patch.object(
                        context.normalization,
                        "current_summary",
                        side_effect=AssertionError("A summary cannot admit recovery"),
                    ),
                ):
                    review_page = client.get(prepare_url, follow_redirects=False)
                self.assertEqual(review_page.status_code, 303, review_page.text)
                self.assertEqual(
                    review_page.headers["location"],
                    f"/workspaces/{workspace_id}/resolution",
                )
                recover.assert_called_once()

                repository = context.preparation_recovery.repository
                database_path = (
                    repository.workspace_directory(workspace_id)
                    / "workspace-engine.duckdb"
                )
                with repository._connect(database_path) as connection:
                    projection = connection.execute(
                        "SELECT projection_json FROM canonical_prepared_projection "
                        "WHERE run_id = ?",
                        [original.staging_run_id],
                    ).fetchone()[0]
                    retired_text = json.dumps(
                        {**json.loads(projection), "contract_version": 3}
                    )
                    connection.execute(
                        "UPDATE canonical_prepared_projection SET projection_json = ? "
                        "WHERE run_id = ?",
                        [retired_text, original.staging_run_id],
                    )
                caller = get_ident()
                read_threads = []
                recover_current = context.preparation_recovery.current_for_review

                def recover_off_loop(
                    *args,
                    _read_threads=read_threads,
                    _recover_current=recover_current,
                    **kwargs,
                ):
                    with self.assertRaises(RuntimeError):
                        get_running_loop()
                    _read_threads.append(get_ident())
                    return _recover_current(*args, **kwargs)

                with (
                    patch.object(
                        context.preparation_recovery,
                        "current_for_review",
                        side_effect=recover_off_loop,
                    ) as recover,
                    patch.object(
                        context.preparation.sessions,
                        "_iter_canonical_rows",
                        side_effect=AssertionError("The page must not restore rows"),
                    ),
                    patch.object(
                        context.artifacts,
                        "materialize_prepared_snapshot",
                        side_effect=AssertionError("The page must not reopen values"),
                    ),
                    collect_duckdb_request_timings() as timings,
                    count_duckdb_statements() as statements,
                ):
                    page = client.get(prepare_url, follow_redirects=False)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn("Prepare all source rows", page.text)
                self.assertIn("Prepare data</button>", page.text)
                recover.assert_called_once()
                self.assertTrue(read_threads)
                self.assertTrue(all(thread != caller for thread in read_threads))
                self.assertLessEqual(timings.connection_count, 8)
                self.assertLessEqual(timings.schema_check_count, 6)
                recovery_queries = [
                    sql for sql in statements if "WITH staging AS (" in sql
                ]
                self.assertEqual(len(recovery_queries), 1)

                manager = context.preparation_jobs
                start_worker = manager._start
                with patch.object(manager, "_start") as start:
                    started = client.post(
                        f"/workspaces/{workspace_id}/summary/check",
                        data={"csrf_token": fixture.csrf},
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                    self.assertEqual(started.status_code, 303, started.text)
                    active = manager.active(workspace_id)
                    self.assertIsNotNone(active)
                    assert active is not None
                    with patch.object(
                        context.preparation_recovery,
                        "current_for_review",
                        side_effect=AssertionError("An active job needs no recovery"),
                    ):
                        active_page = client.get(prepare_url, follow_redirects=False)
                    self.assertEqual(active_page.status_code, 303, active_page.text)
                    self.assertEqual(
                        active_page.headers["location"], started.headers["location"]
                    )
                    repeated = client.post(
                        f"/workspaces/{workspace_id}/summary/check",
                        data={"csrf_token": fixture.csrf},
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                    self.assertEqual(repeated.status_code, 303, repeated.text)
                    self.assertEqual(
                        repeated.headers["location"], started.headers["location"]
                    )
                    start.assert_called_once()
                start_worker(active, actor)
                status = _wait_for_preparation(
                    client, started.headers["location"], timeout=60
                )
                self.assertEqual(status["status"], "SUCCEEDED", status)
                current = context.preparation_recovery.current(
                    workspace_id, original.mapping_content_hash, actor=actor
                )
                self.assertIsNotNone(current)
                assert current is not None
                self.assertNotEqual(current.staging_run_id, original.staging_run_id)
                with repository._connect(database_path) as connection:
                    retained = connection.execute(
                        "SELECT projection_json FROM canonical_prepared_projection "
                        "WHERE run_id = ?",
                        [original.staging_run_id],
                    ).fetchone()[0]
                self.assertEqual(retained, retired_text)

    def test_view_only_navigation_does_not_authorize_command_recovery(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, workspace_id, original = self._prepared_workspace(major)
                context, client = fixture.context, fixture.client
                reader = replace(
                    resolve_local_actor(context),
                    capabilities=frozenset({Capability.PROJECT_VIEW}),
                )
                with (
                    patch.object(
                        context.identity_resolver,
                        "resolve",
                        side_effect=LocalRequestIdentityResolver(reader).resolve,
                    ),
                    patch.object(
                        context.preparation_recovery,
                        "current_for_review",
                        wraps=context.preparation_recovery.current_for_review,
                    ) as recover,
                    collect_duckdb_request_timings() as timings,
                    count_duckdb_statements() as statements,
                ):
                    page = client.get(
                        f"/workspaces/{workspace_id}/prepare", follow_redirects=False
                    )
                self.assertEqual(page.status_code, 303, page.text)
                self.assertEqual(
                    page.headers["location"],
                    f"/workspaces/{workspace_id}/normalization",
                )
                recover.assert_called_once_with(
                    workspace_id, original.mapping_content_hash, actor=reader
                )
                self.assertLessEqual(timings.connection_count, 8)
                self.assertLessEqual(timings.schema_check_count, 6)
                self.assertEqual(
                    sum("WITH staging AS (" in sql for sql in statements), 1
                )

                with (
                    patch.object(
                        context.preparation_recovery.repository,
                        "read_current",
                        side_effect=AssertionError(
                            "Denied recovery must not read data"
                        ),
                    ) as read,
                    patch.object(
                        context.preparation_recovery.mappings.sources,
                        "get_source_selection",
                        side_effect=AssertionError(
                            "Denied recovery must not read inputs"
                        ),
                    ) as sources,
                    self.assertRaises(AuthorizationError),
                ):
                    context.preparation_recovery.current(
                        workspace_id, original.mapping_content_hash, actor=reader
                    )
                read.assert_not_called()
                sources.assert_not_called()

                with (
                    patch.object(
                        context.identity_resolver,
                        "resolve",
                        side_effect=LocalRequestIdentityResolver(reader).resolve,
                    ),
                    patch.object(
                        context.preparation_jobs,
                        "enqueue",
                        side_effect=AssertionError("Denied requests cannot queue work"),
                    ) as enqueue,
                    patch.object(
                        context.preparation_recovery,
                        "current",
                        side_effect=AssertionError(
                            "Denied requests cannot recover work"
                        ),
                    ) as recover_command,
                ):
                    denied = client.post(
                        f"/workspaces/{workspace_id}/summary/check",
                        data={"csrf_token": fixture.csrf},
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                self.assertEqual(denied.status_code, 403, denied.text)
                enqueue.assert_not_called()
                recover_command.assert_not_called()

                with (
                    patch.object(
                        context.identity_resolver,
                        "resolve",
                        side_effect=LocalRequestIdentityResolver(reader).resolve,
                    ),
                    patch.object(
                        context.preparation_recovery.repository,
                        "read_current",
                        return_value=None,
                    ),
                ):
                    prepare = client.get(
                        f"/workspaces/{workspace_id}/prepare", follow_redirects=False
                    )
                self.assertEqual(prepare.status_code, 200, prepare.text)
                self.assertIn("Prepare all source rows", prepare.text)


if __name__ == "__main__":
    unittest.main()
