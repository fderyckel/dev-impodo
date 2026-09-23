"""Representative browser-route qualification for Stage 2 matching rules."""

from __future__ import annotations

import re

from tests.support.browser_scenarios import (
    POST_HEADERS,
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
    def _representative_workspace(self):
        workspace_id, _dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=1,
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
        self.assertIn("<dt>Selected record types</dt><dd>3</dd>", page.text)
        self.assertIn("How should Impodo find an existing Contact?", page.text)
        self.assertIn("How should Impodo find an existing Product?", page.text)
        self.assertIn("How should Impodo find an existing Asset?", page.text)
        self.assertIn("Common Odoo convention", page.text)
        self.assertIn("Enforced by Odoo", page.text)
        self.assertIn("Confirm suggested matching rules", page.text)
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
