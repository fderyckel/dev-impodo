"""Keep Stage 3 locations separate from saved matching decisions and proof."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.presenters.mapping_journey import resolve_mapping_journey
from impodo.web.presenters.navigation import build_workspace_navigation
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.hierarchy_correction import MappingForm
from tests.support.identity import resolve_local_actor


class MappingJourneyLocationTests(unittest.TestCase):
    def test_missing_and_untrusted_locations_use_the_next_starting_point(self):
        for required, expected in ((True, "company"), (False, "tables")):
            for query in ({}, {"step": "<script>", "task": "not-a-task"}):
                with self.subTest(required=required, query=query):
                    journey = resolve_mapping_journey(query, required)
                    self.assertEqual(journey["step"], expected)
                    self.assertEqual(journey["task"], "rows")
                    self.assertEqual(journey["company_required"], required)

    def test_explicit_correction_locations_remain_available(self):
        for task in ("rows", "identity", "fields", "links"):
            with self.subTest(task=task):
                journey = resolve_mapping_journey(
                    {"step": "tables", "task": task},
                    True,
                    mapping_href="/workspaces/example/mapping",
                )
                self.assertEqual(journey["step"], "tables")
                self.assertEqual(journey["task"], task)
                self.assertTrue(journey["company_required"])
                self.assertEqual(
                    journey["steps"][2]["href"],
                    "/workspaces/example/mapping?step=check",
                )


class MappingJourneyBrowserTests(ProjectSetupBrowserTestCase):
    def test_odoo19_local(self):
        self.exercise_journey(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise_journey(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise_journey(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise_journey(20, OdooConnectionMode.REMOTE)

    def exercise_journey(self, major, mode):
        workspace_id, _dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=1,
            relationship_field_count=1,
            odoo_major=major,
            connection_mode=mode,
        )
        context = self.app.state.context
        mapping_url = f"/workspaces/{workspace_id}/mapping"
        before = context.queries.get(workspace_id)
        initial = self.client.get(mapping_url)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.context["mapping_journey"]["step"], "company")
        self.assertEqual(
            initial.context["workspace_navigation"].viewed_page_label,
            "Choose company",
        )
        original_controls = MappingForm(initial.text).entries
        self.assertIn(["target_model_0", "res.partner"], original_controls)
        self.assertIn("relation_value_source_0_0", dict(original_controls))
        self.assertIn("scalar_source_0_1", dict(original_controls))

        for step, label in (
            ("company", "Choose company"),
            ("tables", "Match tables"),
            ("check", "Check and resolve"),
            ("confirm", "Confirm matches"),
        ):
            with self.subTest(step=step):
                page = self.client.get(f"{mapping_url}?step={step}&task=links")
                self.assertEqual(page.status_code, 200, page.text)
                journey = page.context["mapping_journey"]
                self.assertEqual(journey["step"], step)
                self.assertEqual(journey["task"], "links")
                navigation = page.context["workspace_navigation"]
                self.assertEqual(navigation.viewed_page_label, label)
                stage = next(s for s in navigation.stages if s.stage_id == "match")
                self.assertEqual(
                    [p.label for p in stage.pages if p.current], [label]
                )
                self.assertEqual(len(stage.pages), 4)
                self.assertEqual(MappingForm(page.text).entries, original_controls)
                self.assertIsNone(page.context["submission"])
                self.assertFalse(page.context["next_step"]["available"])

        invalid = self.client.get(f"{mapping_url}?step=unknown&task=unknown")
        self.assertEqual(invalid.context["mapping_journey"]["step"], "company")
        self.assertEqual(invalid.context["mapping_journey"]["task"], "rows")
        self.assertEqual(context.queries.get(workspace_id), before)
        self.assertIsNone(context.queries.get_mapping_working_draft(workspace_id))
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.readiness_calls, [])

        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        selected = context.workspace_states.select_odoo_company(
            workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=before.revision,
            selection=OdooCompanySelection(
                schema.connection_target_hash, 7, "Example company"
            ),
        )
        returning = self.client.get(mapping_url)
        self.assertEqual(returning.status_code, 200, returning.text)
        self.assertEqual(returning.context["mapping_journey"]["step"], "tables")
        self.assertEqual(
            returning.context["workspace_navigation"].viewed_page_label,
            "Match tables",
        )
        state_facts = context.navigation.get(selected, actor=resolve_local_actor(context))
        normal_navigation = build_workspace_navigation(
            state_facts, selected, "mapping/page.html"
        )
        self.assertEqual(normal_navigation.viewed_page_label, "Match tables")
        locked_navigation = build_workspace_navigation(
            replace(state_facts, mapping_complete=False),
            selected,
            "mapping/page.html",
            mapping_step="confirm",
        )
        self.assertEqual(locked_navigation.viewed_page_label, "Confirm matches")
        self.assertIsNone(
            next(s for s in locked_navigation.stages if s.stage_id == "prepare").href
        )

    def test_confirmation_location_does_not_bypass_csrf_or_checked_revision(self):
        workspace_id, _dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        context = self.app.state.context
        url = f"/workspaces/{workspace_id}/mapping?step=confirm"
        page = self.client.get(url)
        form = MappingForm(page.text)
        entries = [*form.entries, ["action", "submit"]]
        with patch.object(
            context.mapping_workspace,
            "submit_current",
            side_effect=AssertionError("Unverified matches must not submit"),
        ):
            no_csrf = self.client.post(
                form.action,
                json={"entries": [item for item in entries if item[0] != "csrf_token"]},
                headers=POST_HEADERS,
            )
            self.assertEqual(no_csrf.status_code, 403)
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
        self.assertIsNone(context.queries.get_mapping_submission(workspace_id))


if __name__ == "__main__":
    unittest.main()
