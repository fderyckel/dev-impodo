"""Keep completed-load exports usable without reopening the authoring journey."""

from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.models import MigrationWorkspaceState
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_recipe_comparison_recovery as recipes
from tests.integration.web.test_fresh_data_controls import field
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.identity import resolve_local_actor


class CompletedLoadWorkbookTests(HierarchyWorkflowFixture):
    def test_closed_workspace_can_create_and_download_its_saved_comparison(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._export_closed_comparison(major, mode)

    def _export_closed_comparison(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        report = self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        self.assertEqual(report.status, "READY")
        workspace = self.context.migration_workspaces.get(
            workspace_id, actor=self.actor
        )
        closed = self.context.migration_workspaces.close(
            workspace_id,
            actor=self.actor,
            expected_revision=workspace.optimistic_revision,
        )
        prefix = f"/workspaces/{workspace_id}/summary"
        form = {"csrf_token": self.csrf, "comparison_id": report.run_id}
        with (
            patch.object(
                self.context,
                "readiness_reader",
                side_effect=AssertionError("Export contacted Odoo"),
            ),
            collect_duckdb_request_timings() as timings,
        ):
            created = self.client.post(
                prefix + "/package",
                data=form,
                headers={**POST_HEADERS, "Accept": "application/json"},
                follow_redirects=False,
            )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["message"], "Review workbook created.")
        # Admission owns registry + workspace; the command owns workspace,
        # engine, registry + data version for bound frozen evidence.
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        downloaded = self.client.get(
            prefix + "/workbook",
            params={"comparison_id": report.run_id},
            follow_redirects=False,
        )
        self.assertEqual(downloaded.status_code, 200, downloaded.text[:100])
        self.assertIn("spreadsheetml.sheet", downloaded.headers["content-type"])
        workbook = load_workbook(BytesIO(downloaded.content), read_only=True)
        try:
            self.assertTrue(workbook.sheetnames)
        finally:
            workbook.close()
        stale = self.client.post(
            prefix + "/package",
            data={**form, "comparison_id": "outdated-comparison"},
            headers={**POST_HEADERS, "Accept": "application/json"},
            follow_redirects=False,
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        stale_download = self.client.get(
            prefix + "/workbook?comparison_id=outdated-comparison",
            follow_redirects=False,
        )
        self.assertEqual(stale_download.status_code, 404)
        no_javascript = self.client.post(
            prefix + "/package",
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(no_javascript.status_code, 303)
        self.assertEqual(
            no_javascript.headers["location"],
            f"/workspaces/{workspace_id}/load/outcome#review-workbook",
        )
        invalid_csrf = self.client.post(
            prefix + "/package",
            data={**form, "csrf_token": "invalid"},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(invalid_csrf.status_code, 403)
        # The exception covers only the two workbook actions. Authoring and
        # comparison routes must keep redirecting without changing evidence.
        for path, method in (
            ("/mapping", "GET"),
            ("/summary", "GET"),
            ("/summary/compare", "POST"),
        ):
            redirected = self.client.request(
                method,
                f"/workspaces/{workspace_id}{path}",
                headers=POST_HEADERS,
                data={"csrf_token": self.csrf},
                follow_redirects=False,
            )
            self.assertEqual(redirected.status_code, 303)
            self.assertEqual(
                redirected.headers["location"], f"/workspaces/{workspace_id}/history/{3 if path == '/mapping' else 5}"
            )
        self.assertEqual(
            self.context.migration_workspaces.get(workspace_id, actor=self.actor),
            closed,
        )
        self.assertIs(closed.state, MigrationWorkspaceState.CLOSED)
        self.assertEqual(self.context.preflight.current_report(workspace_id), report)
        self.assertFalse(target.writes)


class CompletedRecipeWorkbookTests(TestCase):
    def test_closed_recipe_application_can_export_for_both_odoo_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture = recipes.RecipeComparisonRecoveryBrowserTests()
                fixture.setUp()
                try:
                    application, reader = fixture._approved_review(
                        odoo_version=f"{major}.0"
                    )
                    context, client = fixture.context, fixture.client
                    actor = resolve_local_actor(context)
                    workspace_id = application.workspace_id
                    report = run_local_operation_sync(
                        context.preflight.compare,
                        workspace_id,
                        reader=reader,
                        actor=actor,
                    )
                    workspace = context.migration_workspaces.get(
                        workspace_id, actor=actor
                    )
                    csrf = field(
                        client.get(f"/projects/{workspace.project_id}"), "csrf_token"
                    )
                    context.migration_workspaces.close(
                        workspace_id,
                        actor=actor,
                        expected_revision=workspace.optimistic_revision,
                    )
                    prefix = f"/workspaces/{workspace_id}/summary"
                    generated = client.post(
                        prefix + "/package",
                        data={"csrf_token": csrf, "comparison_id": report.run_id},
                        headers={**POST_HEADERS, "Accept": "application/json"},
                        follow_redirects=False,
                    )
                    self.assertEqual(generated.status_code, 200, generated.text)
                    downloaded = client.get(
                        prefix + "/workbook",
                        params={"comparison_id": report.run_id},
                        follow_redirects=False,
                    )
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertIn(
                        "spreadsheetml.sheet", downloaded.headers["content-type"]
                    )
                finally:
                    fixture.doCleanups()
