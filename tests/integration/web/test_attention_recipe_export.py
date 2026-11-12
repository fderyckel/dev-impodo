"""Run correction downloads through real Recipe application workspaces."""

from unittest import TestCase

from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_recipe_comparison_recovery as recipes
from tests.integration.web.test_attention_export import workbook_rows
from tests.support.identity import resolve_local_actor


class AttentionRecipeExportTests(TestCase):
    def test_recipe_preparation_and_comparison_for_both_odoo_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture = recipes.RecipeComparisonRecoveryBrowserTests()
                fixture.setUp()
                try:
                    application, reader = fixture._approved_review(
                        odoo_version=f"{major}.0"
                    )
                    workspace_id = application.workspace_id
                    url = f"/workspaces/{workspace_id}/summary/attention.xlsx"
                    before = fixture.client.get(url)
                    self.assertEqual(
                        before.status_code,
                        200,
                        before.text[:300] if before.status_code != 200 else "",
                    )
                    self.assertEqual(
                        dict(workbook_rows(before)["Overview"])["Odoo check"],
                        "Odoo comparison not completed",
                    )
                    run_local_operation_sync(
                        fixture.context.preflight.compare,
                        workspace_id,
                        reader=reader,
                        actor=resolve_local_actor(fixture.context),
                    )
                    after = fixture.client.get(url)
                    self.assertEqual(
                        after.status_code,
                        200,
                        after.text[:300] if after.status_code != 200 else "",
                    )
                    self.assertEqual(
                        dict(workbook_rows(after)["Overview"])["Odoo check"],
                        "Odoo comparison completed",
                    )
                finally:
                    fixture.doCleanups()
