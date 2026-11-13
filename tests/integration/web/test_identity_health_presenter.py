from __future__ import annotations

from types import SimpleNamespace
import unittest

from impodo.domain.matching_order import (
    MatchingIdentityCheckStatus,
    MatchingIdentityNotCheckableReason,
    MatchingIdentityResult,
)
from impodo.web.presenters.identity_health import identity_health_view


class IdentityHealthPresenterTests(unittest.TestCase):
    def test_missing_source_or_target_components_are_not_presented_as_chosen(self):
        for missing, expected in (
            ("source_identity_column_keys", "Unique row identifier"),
            ("target_identity", "source values for the Odoo matching rule"),
        ):
            with self.subTest(missing=missing):
                dataset = _dataset_view()
                setattr(dataset["mapping"], missing, ())
                view = identity_health_view(
                    workspace_id="workspace:test", dataset_views=(dataset,),
                    live_check=None, live_check_current=False,
                    check_attempt=None, working_draft_is_current=True,
                )
                card = view["cards"][0]
                self.assertEqual(card["status_label"], "Incomplete identity")
                self.assertIn(expected, card["message"])
                self.assertFalse(card["checked"])

    def test_checked_identity_projects_aggregate_counts_and_safe_action(self) -> None:
        result = MatchingIdentityResult(
            dataset_id="products",
            target_model="product.template",
            target_fields=("default_code",),
            status=MatchingIdentityCheckStatus.CHECKED,
            source_row_count=4,
            source_unique_row_count=3,
            source_blank_row_count=1,
            target_unique_key_count=2,
            expected_new_count=1,
            expected_existing_count=2,
            blocked_count=1,
        )

        view = identity_health_view(
            workspace_id="workspace:test",
            dataset_views=(_dataset_view(),),
            live_check=SimpleNamespace(
                check_id="10000000-0000-4000-8000-000000000001",
                identity_results=(result,),
                schema_changed=False,
            ),
            live_check_current=True,
            check_attempt=None,
            working_draft_is_current=True,
        )

        self.assertIsNotNone(view)
        card = view["cards"][0]
        self.assertEqual(card["status_label"], "Tested - needs attention")
        self.assertEqual(card["blocked_count"], 1)
        self.assertEqual(card["expected_existing_count"], 2)
        self.assertEqual(
            card["edit_url"],
            "/workspaces/workspace:test/mapping?mapping_dataset=0"
            "#target-identity-0",
        )

    def test_related_scope_is_explicitly_not_tested(self) -> None:
        result = MatchingIdentityResult(
            dataset_id="products",
            target_model="product.template",
            target_fields=("name", "categ_id"),
            status=MatchingIdentityCheckStatus.NOT_CHECKABLE,
            not_checkable_reason=(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            ),
        )

        view = identity_health_view(
            workspace_id="workspace:test",
            dataset_views=(_dataset_view(),),
            live_check=SimpleNamespace(
                check_id="10000000-0000-4000-8000-000000000002",
                identity_results=(result,),
                schema_changed=False,
            ),
            live_check_current=True,
            check_attempt=None,
            working_draft_is_current=True,
        )

        card = view["cards"][0]
        self.assertEqual(card["status_label"], "Chosen - not tested")
        self.assertIn("related Odoo record", card["message"])
        self.assertFalse(card["checked"])

    def test_unconfirmed_rules_open_odoo_data_for_the_next_decision(self) -> None:
        result = MatchingIdentityResult(
            dataset_id="products", target_model="product.template",
            target_fields=("default_code", "categ_id"),
            status=MatchingIdentityCheckStatus.NOT_CHECKABLE,
            not_checkable_reason=MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE,
        )
        view = identity_health_view(
            workspace_id="workspace:test", dataset_views=(_dataset_view(),),
            live_check=SimpleNamespace(check_id="check", identity_results=(result,), schema_changed=False),
            live_check_current=True, check_attempt=None, working_draft_is_current=True,
        )
        card = view["cards"][0]
        self.assertEqual(card["rule_confirmation_url"], "/workspaces/workspace:test/schema#schema-governance")
        self.assertIn("linked records", card["message"])


def _dataset_view():
    key = SimpleNamespace(
        key_id="product:default-code",
        key_fields=("default_code",),
        scope_fields=(),
        description="Internal Reference",
        recommendation_basis="ODOO_ENFORCED",
    )
    return {
        "index": 0,
        "source": SimpleNamespace(dataset_id="products", name="Products"),
        "mapping": SimpleNamespace(source_identity_column_keys=("row",), target_identity=("ref",)),
        "selected_key": key,
        "matching_rule_labels": {key.key_id: "Internal Reference"},
        "model": SimpleNamespace(label="Product"),
        "selected_model": "product.template",
        "odoo_pinned": False,
        "edit_url": "/workspaces/workspace:test/mapping?mapping_dataset=0",
    }


if __name__ == "__main__":
    unittest.main()
