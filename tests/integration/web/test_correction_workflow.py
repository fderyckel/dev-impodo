"""Browser evidence for the focused completed-load correction journey."""

from __future__ import annotations

from impodo.application.correction_workflow import CorrectionJourneyView
from impodo.domain.correction import (
    CorrectionPlanSummary,
    CorrectionPlanSummaryGroup,
)

from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


class _CorrectionViews:
    def __init__(self, view: CorrectionJourneyView) -> None:
        self.view = view

    def get(self, completed_workspace_id: str, *, actor):
        return (
            self.view
            if completed_workspace_id == self.view.completed_workspace_id
            else None
        )

    def list_for_project(self, project_id: str, *, actor):
        return (self.view,) if project_id == self.view.project_id else ()

    def binding_for_successor(self, successor_workspace_id: str):
        return None


class CorrectionBrowserTests(ProjectSetupBrowserTestCase):
    def _closed_workspace(self):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Completed products load",
                "source_mode": "FILE",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        context = self.app.state.context
        workspace = context.migration_workspaces.get(
            workspace_id,
            actor=context.actor,
        )
        closed = context.migration_workspaces.close(
            workspace_id,
            actor=context.actor,
            expected_revision=workspace.optimistic_revision,
        )
        return closed

    def test_only_eligible_completed_load_gets_the_correction_action(self) -> None:
        closed = self._closed_workspace()
        context = self.app.state.context
        view = CorrectionJourneyView(
            project_id=closed.project_id,
            completed_workspace_id=closed.workspace_id,
            successor_workspace_id=None,
            has_current_plan=False,
            has_confirmation=False,
            completed=False,
            plan_summary=None,
        )
        context.corrections = _CorrectionViews(view)

        page = self.client.get(f"/projects/{closed.project_id}")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Correct this Odoo load", page.text)
        self.assertIn(
            f'/workspaces/{closed.workspace_id}/correction',
            page.text,
        )

    def test_old_mapping_url_redirects_to_the_safe_successor_explanation(self) -> None:
        closed = self._closed_workspace()
        context = self.app.state.context
        context.corrections = _CorrectionViews(
            CorrectionJourneyView(
                project_id=closed.project_id,
                completed_workspace_id=closed.workspace_id,
                successor_workspace_id=None,
                has_current_plan=False,
                has_confirmation=False,
                completed=False,
                plan_summary=None,
            )
        )

        response = self.client.get(
            f"/workspaces/{closed.workspace_id}/mapping",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/workspaces/{closed.workspace_id}/correction",
        )

    def test_compact_review_contains_counts_but_no_protected_values_or_ids(self) -> None:
        closed = self._closed_workspace()
        context = self.app.state.context
        context.corrections = _CorrectionViews(
            CorrectionJourneyView(
                project_id=closed.project_id,
                completed_workspace_id=closed.workspace_id,
                successor_workspace_id=closed.workspace_id,
                has_current_plan=True,
                has_confirmation=False,
                completed=False,
                plan_summary=CorrectionPlanSummary(
                    field_count=3,
                    record_count=2,
                    groups=(
                        CorrectionPlanSummaryGroup(
                            dataset="Products",
                            target_model="product.template",
                            target_field="active",
                            changed_field_count=3,
                        ),
                    ),
                ),
            )
        )

        page = self.client.get(
            f"/workspaces/{closed.workspace_id}/correction"
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn("2 exact records, 3 field corrections", page.text)
        self.assertIn("Zero creates", page.text)
        self.assertIn("Apply 2 record corrections", page.text)
        self.assertNotIn("987654", page.text)
        self.assertNotIn("read-secret", page.text)
        self.assertNotIn("write-secret", page.text)


if __name__ == "__main__":
    import unittest

    unittest.main()
