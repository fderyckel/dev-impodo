"""Verify structured Recipe blockers lead to their owning workflow page."""

from __future__ import annotations

from dataclasses import replace
import unittest

from impodo.projects import WorkspaceState, ProjectStatus
from impodo.recipes import RecipeDraftIssue, RecipeDraftRecoveryStep
from impodo.web.presenters.recipe import build_recipe_draft_recovery_view


class RecipeDraftRecoveryPresenterTests(unittest.TestCase):
    @staticmethod
    def _project() -> WorkspaceState:
        return WorkspaceState(
            project_id="project-1",
            name="Customer migration",
            source_system="CSV export",
            status=ProjectStatus.REGISTERED,
        )

    def test_each_compiler_step_has_one_named_action(self):
        project = self._project()
        cases = (
            (RecipeDraftRecoveryStep.SOURCE_DATA, "datasets", "Review Source data"),
            (RecipeDraftRecoveryStep.ODOO_DATA, "schema", "Review Odoo data"),
            (RecipeDraftRecoveryStep.MATCH_DATA, "mapping", "Review Match data"),
            (RecipeDraftRecoveryStep.PREPARE_DATA, "prepare", "Review Prepare data"),
        )

        for step, page, label in cases:
            with self.subTest(step=step):
                issue = RecipeDraftIssue("BLOCKED", "Message.", "Recovery.", step)
                view = build_recipe_draft_recovery_view("recipe-1", project, issue)
                self.assertEqual(
                    view.href,
                    f"/projects/{project.project_id}/{page}",
                )
                self.assertEqual(view.action_label, label)

    def test_support_reference_is_preserved_for_progressive_disclosure(self):
        project = self._project()
        issue = RecipeDraftIssue(
            "ODOO_FIELD_EVIDENCE_REQUIRED",
            "Message.",
            "Recovery.",
            RecipeDraftRecoveryStep.ODOO_DATA,
            "res.partner.country_id",
        )

        view = build_recipe_draft_recovery_view("recipe-1", project, issue)

        self.assertEqual(view.support_reference, "res.partner.country_id")

    def test_recipe_level_recovery_does_not_require_a_workspace(self):
        cases = (
            (
                RecipeDraftRecoveryStep.RECIPE_OVERVIEW,
                "/recipes/recipe-1",
                "Review Recipe",
            ),
            (
                RecipeDraftRecoveryStep.RECIPE_APPLICATION,
                "/recipes/recipe-1/application",
                "Review application",
            ),
            (
                RecipeDraftRecoveryStep.NEW_PROJECT,
                "/recipes/new",
                "Create a new project",
            ),
        )

        for step, href, label in cases:
            with self.subTest(step=step):
                issue = RecipeDraftIssue("BLOCKED", "Message.", "Recovery.", step)
                view = build_recipe_draft_recovery_view("recipe-1", None, issue)
                self.assertEqual(view.href, href)
                self.assertEqual(view.action_label, label)

    def test_incomplete_project_returns_to_setup_before_later_recovery(self):
        project = replace(self._project(), status=ProjectStatus.DRAFT)
        issue = RecipeDraftIssue(
            "BLOCKED",
            "Message.",
            "Recovery.",
            RecipeDraftRecoveryStep.MATCH_DATA,
        )

        view = build_recipe_draft_recovery_view("recipe-1", project, issue)

        self.assertEqual(view.href, f"/projects/{project.project_id}/details")
        self.assertEqual(view.action_label, "Complete Recipe setup")


if __name__ == "__main__":
    unittest.main()
