"""Representative browser-route qualification for Stage 2 matching rules."""

from __future__ import annotations

import re

from tests.support.browser_scenarios import (
    POST_HEADERS,
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.stage2_matching_rules import (
    install_stage2_schema,
    matching_rule_form,
    representative_stage2_models,
    representative_stage2_selections,
    unresolved_stage2_model,
)


class Stage2MatchingRuleJourneyTests(ProjectSetupBrowserTestCase):
    def _representative_workspace(
        self, *, odoo_major=19, connection_mode=OdooConnectionMode.REMOTE,
    ):
        workspace_id, _dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=1,
            odoo_major=odoo_major,
            connection_mode=connection_mode,
        )
        schema = install_stage2_schema(
            self,
            workspace_id,
            representative_stage2_models(),
            hash_character="7",
        )
        return workspace_id, schema

    def _connector_calls(self):
        return (
            tuple(self.connection_calls),
            tuple(self.model_catalog_calls),
            tuple(self.schema_calls),
            tuple(self.read_identity_calls),
            tuple(self.readiness_calls),
        )

    def test_substeps_keep_every_rule_and_do_not_change_saved_evidence(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(odoo_major=major, connection_mode=mode.value):
                    workspace_id, schema = self._representative_workspace(
                        odoo_major=major, connection_mode=mode,
                    )
                    queries = self.app.state.context.queries
                    source = queries.get_source_selection(workspace_id)
                    before_review = self._connector_calls()
                    for step in ("records", "related", "matching"):
                        page = self.client.get(
                            f"/workspaces/{workspace_id}/schema?step={step}",
                            follow_redirects=False,
                        )
                        self.assertEqual(page.status_code, 200, page.text)
                        self.assertIn("data-schema-journey", page.text)
                        self.assertIn(f'data-step="{step}"', page.text)
                        for label in (
                            "Choose records", "Related values", "Matching rules",
                        ):
                            self.assertIn(label, page.text)
                        for index in range(len(schema.models)):
                            self.assertIn(
                                f'name="primary_key_field_{index}"', page.text,
                            )
                        self.assertEqual(
                            queries.get_odoo_schema_catalog(workspace_id), schema,
                        )
                        self.assertEqual(
                            queries.get_source_selection(workspace_id), source,
                        )
                        self.assertIsNone(
                            queries.get_schema_governance(workspace_id),
                        )
                        self.assertEqual(self._connector_calls(), before_review)

    def test_standard_extended_and_custom_rules_confirm_once_without_odoo(
        self,
    ) -> None:
        workspace_id, schema = self._representative_workspace()
        before_review = self._connector_calls()

        page = self.client.get(f"/workspaces/{workspace_id}/schema")

        self.assertEqual(page.status_code, 200)
        self.assertIn("<dt>Suggested</dt><dd>3</dd>", page.text)
        self.assertIn("<dt>Confirmed</dt><dd>0</dd>", page.text)
        self.assertIn("<dt>Needs attention</dt><dd>0</dd>", page.text)
        self.assertIn("<dt>Record types</dt><dd>3</dd>", page.text)
        self.assertIn("How should Impodo find an existing Contact?", page.text)
        self.assertIn("How should Impodo find an existing Product?", page.text)
        self.assertIn("How should Impodo find an existing Asset?", page.text)
        self.assertIn("Common Odoo convention", page.text)
        self.assertIn("Enforced by Odoo", page.text)
        self.assertIn("Confirm suggested matching rules and continue", page.text)
        self.assertEqual(self._connector_calls(), before_review)

        confirmed = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data=matching_rule_form(
                self.csrf,
                schema,
                representative_stage2_selections(),
            ),
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(confirmed.status_code, 303)
        self.assertEqual(
            confirmed.headers["location"],
            f"/workspaces/{workspace_id}/mapping",
        )
        self.assertEqual(self._connector_calls(), before_review)
        governance = self.app.state.context.queries.get_schema_governance(
            workspace_id
        )
        self.assertIsNotNone(governance)
        assert governance is not None
        by_model = {
            item.model: item for item in governance.business_keys
        }
        self.assertEqual(
            set(by_model),
            {"res.partner", "product.template", "x.asset"},
        )
        self.assertEqual(
            {
                model: item.recommendation_basis
                for model, item in by_model.items()
            },
            {
                "res.partner": "CURATED_CONVENTION",
                "product.template": "CURATED_CONVENTION",
                "x.asset": "ODOO_ENFORCED",
            },
        )

    def test_advanced_override_survives_error_confirmation_and_reload(
        self,
    ) -> None:
        workspace_id, schema = self._representative_workspace()
        selections = representative_stage2_selections()
        selections["product.template"] = (
            ("x_legacy_code",),
            (),
            "Approved legacy product reference",
        )
        selections["x.asset"] = (
            ("code",),
            ("code",),
            "Invalid repeated field",
        )

        rejected = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data=matching_rule_form(self.csrf, schema, selections),
            headers=POST_HEADERS,
        )

        self.assertEqual(rejected.status_code, 422)
        self.assertRegex(
            rejected.text,
            re.compile(
                r"How should Impodo find an existing Product\?.*?"
                r"Changed from suggestion.*?Legacy Reference",
                re.DOTALL,
            ),
        )
        self.assertIn("Approved legacy product reference", rejected.text)
        self.assertIsNone(
            self.app.state.context.queries.get_schema_governance(workspace_id)
        )

        selections["x.asset"] = (("code",), (), "Unique asset code")
        saved = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data=matching_rule_form(self.csrf, schema, selections),
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303)

        reopened = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertRegex(
            reopened.text,
            re.compile(
                r"How should Impodo find an existing Product\?.*?"
                r"Confirmed matching rule.*?Legacy Reference",
                re.DOTALL,
            ),
        )
        governance = self.app.state.context.queries.get_schema_governance(
            workspace_id
        )
        self.assertIsNotNone(governance)
        assert governance is not None
        product = next(
            item
            for item in governance.business_keys
            if item.model == "product.template"
        )
        self.assertEqual(product.key_fields, ("x_legacy_code",))
        self.assertEqual(product.recommendation_basis, "")
        self.assertIsNone(product.recommendation_policy_version)

    def test_confirmed_rule_can_be_changed_and_confirmed_again(self) -> None:
        workspace_id, schema = self._representative_workspace()
        selections = representative_stage2_selections()
        first_confirmation = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data=matching_rule_form(self.csrf, schema, selections),
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(first_confirmation.status_code, 303)

        reopened = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertRegex(
            reopened.text,
            re.compile(
                r"How should Impodo find an existing Product\?.*?"
                r"Confirmed matching rule.*?Internal Reference.*?"
                r"Confirm matching rules and continue",
                re.DOTALL,
            ),
        )
        self.assertIn("data-key-confirmed=\"true\"", reopened.text)
        self.assertNotIn("data-key-card-submit", reopened.text)

        selections["product.template"] = (
            ("x_legacy_code",),
            (),
            "Approved legacy product reference",
        )
        changed = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data=matching_rule_form(self.csrf, schema, selections),
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 303)
        self.assertEqual(
            changed.headers["location"],
            f"/workspaces/{workspace_id}/mapping",
        )

        governance = self.app.state.context.queries.get_schema_governance(
            workspace_id
        )
        self.assertIsNotNone(governance)
        assert governance is not None
        product = next(
            item
            for item in governance.business_keys
            if item.model == "product.template"
        )
        self.assertEqual(product.key_fields, ("x_legacy_code",))
        self.assertEqual(
            product.description,
            "Approved legacy product reference",
        )
        self.assertEqual(product.recommendation_basis, "")
        self.assertIsNone(product.recommendation_policy_version)

    def test_unresolved_custom_model_is_visible_and_blocks_completion(
        self,
    ) -> None:
        workspace_id, _schema = self._representative_workspace()
        schema = install_stage2_schema(
            self,
            workspace_id,
            (*representative_stage2_models(), unresolved_stage2_model()),
            hash_character="8",
        )
        before_review = self._connector_calls()

        page = self.client.get(f"/workspaces/{workspace_id}/schema")

        self.assertIn("<dt>Suggested</dt><dd>3</dd>", page.text)
        self.assertIn("<dt>Needs attention</dt><dd>1</dd>", page.text)
        self.assertRegex(
            page.text,
            re.compile(
                r"How should Impodo find an existing Unresolved Custom Record\?"
                r".*?Needs attention.*?no single safe recommendation",
                re.DOTALL | re.IGNORECASE,
            ),
        )
        self.assertEqual(self._connector_calls(), before_review)

        selections = representative_stage2_selections()
        blocked = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data=matching_rule_form(self.csrf, schema, selections),
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn(
            "Choose a matching rule for Unresolved Custom Record.",
            blocked.text,
        )
        self.assertIsNone(
            self.app.state.context.queries.get_schema_governance(workspace_id)
        )
        self.assertEqual(self._connector_calls(), before_review)


if __name__ == "__main__":
    import unittest

    unittest.main()
