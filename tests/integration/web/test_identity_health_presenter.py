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
        "mapping": SimpleNamespace(),
        "selected_key": key,
        "matching_rule_labels": {key.key_id: "Internal Reference"},
        "model": SimpleNamespace(label="Product"),
        "selected_model": "product.template",
        "odoo_pinned": False,
        "edit_url": "/workspaces/workspace:test/mapping?mapping_dataset=0",
    }


if __name__ == "__main__":
    unittest.main()
