"""Keep Stage 2 locations separate from saved decisions and Odoo reads."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from impodo.application.recipe.odoo_requirements import (
    OdooCheckModelRequirement,
    OdooCheckRequirementPlan,
)
from impodo.application.workspace.navigation import WorkspaceNavigationFacts
from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)
from impodo.web.presenters.navigation import build_workspace_navigation
from impodo.web.presenters.schema import _render
from impodo.web.presenters.schema_journey import resolve_schema_journey
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
)
from tests.support.stage2_matching_rules import matching_rule_form


class SchemaJourneyLocationTests(unittest.TestCase):
    def test_passive_locations_do_not_complete_missing_decisions(self):
        workspace = WorkspaceState(
            workspace_id="workspace:stage2",
            name="Data",
            source_system="CSV",
            source_mode=SourceMode.FILE,
            status=WorkspaceStatus.REGISTERED,
        )
        facts = WorkspaceNavigationFacts(
            workspace_id=workspace.workspace_id,
            source_selection_hash="accepted",
        )
        for step in ("records", "related", "matching"):
            with self.subTest(step=step):
                journey = resolve_schema_journey(
                    {"step": step},
                    enabled=True,
                    schema_href=f"/workspaces/{workspace.workspace_id}/schema",
                )
                navigation = build_workspace_navigation(
                    facts,
                    workspace,
                    "workspace_schema.html",
                    schema_journey=journey,
                )
                stage = next(
                    stage for stage in navigation.stages if stage.stage_id == "odoo"
                )
                self.assertEqual(
                    [page.page_id for page in stage.pages if page.current],
                    [f"schema-{step}"],
                )
                self.assertFalse(any(page.status == "complete" for page in stage.pages))
                self.assertEqual(stage.status, "current")
                matching = next(
                    stage for stage in navigation.stages if stage.stage_id == "match"
                )
                self.assertIsNone(matching.href)
                self.assertFalse(facts.schema_complete)

    def test_normal_return_opens_first_unfinished_responsibility(self):
        cases = (
            ({}, "records"),
            ({"records_complete": True}, "related"),
            ({"records_complete": True, "related_complete": True}, "matching"),
            ({"records_complete": True, "related_optional": True}, "matching"),
            ({"records_complete": True, "records_attention": True}, "records"),
        )
        for evidence, step in cases:
            with self.subTest(evidence=evidence):
                journey = resolve_schema_journey({}, enabled=True, **evidence)
                self.assertEqual(journey["step"], step)

    def test_invalid_location_does_not_override_required_work(self):
        journey = resolve_schema_journey(
            {"step": "confirm-everything"},
            enabled=True,
            records_complete=True,
        )
        self.assertEqual(journey["step"], "related")

    def test_error_panel_overrides_stale_view_parameter(self):
        for owner in ("records", "related", "matching"):
            journey = resolve_schema_journey(
                {"step": "matching"},
                enabled=True,
                preferred_step=owner,
            )
            self.assertEqual(journey["step"], owner)

    def test_optional_choices_are_distinct_from_confirmed_work(self):
        journey = resolve_schema_journey(
            {},
            enabled=True,
            records_complete=True,
            related_optional=True,
        )
        steps = {step["key"]: step for step in journey["steps"]}
        self.assertEqual(steps["related"]["status_label"], "Optional")
        self.assertEqual(steps["matching"]["status_label"], "Available")
        self.assertEqual(journey["step"], "matching")

    def test_recovery_attention_wins_over_old_completion(self):
        journey = resolve_schema_journey(
            {},
            enabled=True,
            records_complete=True,
            related_complete=True,
            related_attention=True,
        )
        self.assertEqual(journey["step"], "related")
        self.assertEqual(journey["steps"][1]["status_label"], "Needs attention")

    def test_other_journeys_do_not_receive_authoring_tabs(self):
        journey = resolve_schema_journey({"step": "matching"}, enabled=False)
        self.assertFalse(journey["enabled"])
        self.assertEqual(journey["steps"], ())


class SchemaJourneyBrowserTests(ProjectSetupBrowserTestCase):
    def test_recipe_requirements_keep_their_read_only_presentation(self):
        plan = OdooCheckRequirementPlan(
            models=(
                OdooCheckModelRequirement(
                    "res.partner", ("ref", "name"), ("Saved Recipe",)
                ),
            ),
            supporting_values=(),
        )
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, _, _ = self._mapping_ready_workspace(
                    scalar_field_count=1,
                    odoo_major=major,
                    connection_mode=OdooConnectionMode.REMOTE,
                )
                context = self.app.state.context
                with (
                    patch.object(
                        context.run_setups,
                        "odoo_check_requirements_for_page",
                        return_value=plan,
                    ),
                    patch(
                        "impodo.web.presenters.schema._render", wraps=_render
                    ) as render,
                ):
                    response = self.client.get(
                        f"/workspaces/{workspace_id}/schema?step=matching"
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(render.call_args.kwargs["schema_journey"]["enabled"])
                self.assertNotIn("data-schema-journey", response.text)
                self.assertNotIn("data-model-picker", response.text)
                self.assertIn("Saved Recipe", response.text)
                self.assertIn("Check this Odoo", response.text)

    def test_capture_failure_reveals_records_without_discarding_choices(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, _, _ = self._mapping_ready_workspace(
                    scalar_field_count=1,
                    odoo_major=major,
                    connection_mode=OdooConnectionMode.REMOTE,
                )
                context = self.app.state.context
                original = context.queries.get_odoo_schema_catalog(workspace_id)
                with (
                    patch.object(
                        context,
                        "schema_reader",
                        side_effect=ConnectorTransportError("The field request failed"),
                    ) as reader,
                    patch(
                        "impodo.web.presenters.schema._render", wraps=_render
                    ) as render,
                ):
                    response = self.client.post(
                        f"/workspaces/{workspace_id}/schema/capture?step=matching",
                        data={"csrf_token": self.csrf},
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                self.assertEqual(response.status_code, 422, response.text)
                reader.assert_called_once()
                journey = render.call_args.kwargs["schema_journey"]
                self.assertEqual(journey["step"], "records")
                self.assertEqual(journey["steps"][0]["status_label"], "Needs attention")
                self.assertIn("Try loading details again", response.text)
                self.assertEqual(
                    context.queries.get_odoo_schema_catalog(workspace_id), original
                )

    def test_pending_odoo_changes_reveal_records_before_replacing_evidence(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, _, _ = self._mapping_ready_workspace(
                    scalar_field_count=1,
                    odoo_major=major,
                    connection_mode=OdooConnectionMode.REMOTE,
                )
                context = self.app.state.context
                workspace = context.queries.get(workspace_id)
                original = context.queries.get_odoo_schema_catalog(workspace_id)
                governance = context.queries.get_schema_governance(workspace_id)
                snapshot = _browser_schema(workspace)
                partner = snapshot.models["res.partner"]
                snapshot = replace(
                    snapshot,
                    fingerprint=replace(
                        snapshot.fingerprint,
                        odoo_version=f"{major}.0",
                        module_versions={"base": f"{major}.0.1.0"},
                    ),
                    models={
                        **snapshot.models,
                        "res.partner": replace(
                            partner,
                            fields={
                                **partner.fields,
                                "name": replace(partner.fields["name"], required=False),
                            },
                        ),
                    },
                )
                with (
                    patch.object(
                        context, "schema_reader", return_value=snapshot
                    ) as reader,
                    patch(
                        "impodo.web.presenters.schema._render", wraps=_render
                    ) as render,
                ):
                    checked = self.client.post(
                        f"/workspaces/{workspace_id}/schema/capture",
                        data={"csrf_token": self.csrf},
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                self.assertEqual(
                    checked.status_code,
                    303,
                    render.call_args.kwargs.get("support_error")
                    if render.called
                    else checked.text,
                )
                reader.assert_called_once()
                with patch(
                    "impodo.web.presenters.schema._render", wraps=_render
                ) as render:
                    response = self.client.get(
                        f"/workspaces/{workspace_id}/schema?step=matching"
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    render.call_args.kwargs["schema_journey"]["step"], "records"
                )
                self.assertIn("Use updated Odoo details", response.text)
                pending = context.queries.get_odoo_schema_catalog(workspace_id)
                self.assertEqual(pending.content_hash, original.content_hash)
                self.assertIsNotNone(pending.pending_refresh)
                self.assertEqual(
                    context.queries.get_schema_governance(workspace_id), governance
                )

    def test_odoo19_local_navigation_preserves_saved_evidence(self):
        self.exercise_navigation(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote_navigation_preserves_saved_evidence(self):
        self.exercise_navigation(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local_navigation_preserves_saved_evidence(self):
        self.exercise_navigation(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote_navigation_preserves_saved_evidence(self):
        self.exercise_navigation(20, OdooConnectionMode.REMOTE)

    def exercise_navigation(self, major, mode):
        workspace_id, _, _ = self._mapping_ready_workspace(
            scalar_field_count=1,
            odoo_major=major,
            connection_mode=mode,
        )
        queries = self.app.state.context.queries
        schema = queries.get_odoo_schema_catalog(workspace_id)
        governance = queries.get_schema_governance(workspace_id)
        selection = queries.get_source_selection(workspace_id)
        before_calls = (
            len(self.schema_calls),
            len(self.read_identity_calls),
            len(self.readiness_calls),
        )
        with patch("impodo.web.presenters.schema._render", wraps=_render) as render:
            initial = self.client.get(f"/workspaces/{workspace_id}/schema")
            self.assertEqual(initial.status_code, 200, initial.text)
            self.assertEqual(
                render.call_args.kwargs["schema_journey"]["step"], "matching"
            )
        for step in ("records", "related", "matching"):
            with (
                self.subTest(step=step),
                patch("impodo.web.presenters.schema._render", wraps=_render) as render,
            ):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/schema?step={step}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                journey = render.call_args.kwargs["schema_journey"]
                self.assertTrue(journey["enabled"])
                self.assertEqual(journey["step"], step)
                self.assertEqual(
                    [item["label"] for item in journey["steps"]],
                    ["Choose records", "Related values", "Matching rules"],
                )
                self.assertEqual(queries.get_odoo_schema_catalog(workspace_id), schema)
                self.assertEqual(
                    queries.get_schema_governance(workspace_id), governance
                )
                self.assertEqual(queries.get_source_selection(workspace_id), selection)
        self.assertEqual(
            (
                len(self.schema_calls),
                len(self.read_identity_calls),
                len(self.readiness_calls),
            ),
            before_calls,
        )

    def test_failed_confirmation_reveals_matching_and_keeps_submitted_values(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, _, _ = self._mapping_ready_workspace(
                    scalar_field_count=1, odoo_major=major
                )
                queries = self.app.state.context.queries
                schema = queries.get_odoo_schema_catalog(workspace_id)
                original_governance = queries.get_schema_governance(workspace_id)
                selections = {
                    model.name: (("ref",), ("ref",), "Keep this description")
                    for model in schema.models
                }
                form = matching_rule_form(self.csrf, schema, selections)
                with patch(
                    "impodo.web.presenters.schema._render", wraps=_render
                ) as render:
                    response = self.client.post(
                        f"/workspaces/{workspace_id}/schema/govern?step=records",
                        data=form,
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    render.call_args.kwargs["schema_journey"]["step"], "matching"
                )
                self.assertIn("Keep this description", response.text)
                self.assertEqual(
                    queries.get_schema_governance(workspace_id), original_governance
                )
