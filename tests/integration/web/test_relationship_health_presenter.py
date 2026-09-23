from __future__ import annotations

from types import SimpleNamespace
import unittest

from impodo.domain.mapping.contracts import ResolverOrigin
from impodo.domain.relationship_health import (
    RelationshipHealthNotCheckableReason,
    RelationshipHealthResult,
    RelationshipHealthStatus,
)
from impodo.web.presenters.relationship_health import relationship_health_view


class RelationshipHealthPresenterTests(unittest.TestCase):
    def test_required_parent_card_names_origin_counts_and_correction_routes(self) -> None:
        owner, parent = _dataset_views()
        result = RelationshipHealthResult(
            owner_dataset_id="bom-lines",
            target_field="bom_id",
            relationship_kind="many2one",
            resolver_origin=ResolverOrigin.TARGET_THEN_DATASET,
            related_model="mrp.bom",
            dependency_dataset_id="boms",
            required=True,
            status=RelationshipHealthStatus.CHECKED,
            source_row_count=5,
            populated_choice_count=3,
            blank_row_count=1,
            incomplete_row_count=1,
            target_count=1,
            incoming_count=1,
            missing_count=1,
        )

        view = relationship_health_view(
            workspace_id="workspace:test",
            dataset_views=(owner, parent),
            live_check=SimpleNamespace(
                relationship_health_results=(result,),
                schema_changed=False,
            ),
            live_check_current=True,
            check_attempt=None,
            working_draft_is_current=True,
        )

        card = view["cards"][0]
        self.assertEqual(card["status_label"], "Needs attention")
        self.assertEqual(card["blocked_count"], 3)
        self.assertIn("BOM Lines", card["message"])
        self.assertIn("incoming BOMs", card["message"])
        self.assertEqual(card["edit_label"], "Review 3 unresolved choices")
        self.assertEqual(card["related_action_label"], "Open incoming BOMs")
        self.assertIn("#relationship-0-bom_id", card["edit_url"])

    def test_current_complete_relationship_is_ready(self) -> None:
        owner, parent = _dataset_views()
        result = RelationshipHealthResult(
            owner_dataset_id="bom-lines",
            target_field="bom_id",
            relationship_kind="many2one",
            resolver_origin=ResolverOrigin.TARGET_THEN_DATASET,
            related_model="mrp.bom",
            dependency_dataset_id="boms",
            required=True,
            status=RelationshipHealthStatus.CHECKED,
            source_row_count=2,
            populated_choice_count=2,
            target_count=1,
            incoming_count=1,
        )

        view = relationship_health_view(
            workspace_id="workspace:test",
            dataset_views=(owner, parent),
            live_check=SimpleNamespace(
                relationship_health_results=(result,),
                schema_changed=False,
            ),
            live_check_current=True,
            check_attempt=None,
            working_draft_is_current=True,
        )

        card = view["cards"][0]
        self.assertEqual(card["status_label"], "Ready")
        self.assertTrue(card["ready"])
        self.assertIn("exactly one Bill of Material", card["message"])

    def test_one2many_points_to_the_child_inverse_many2one(self) -> None:
        owner, child = _dataset_views()
        one2many = SimpleNamespace(
            target_field="line_ids",
            kind="one2many",
            required=False,
            required_on_create=False,
            resolver=SimpleNamespace(
                origin=ResolverOrigin.TARGET_CATALOG,
                dataset_id=None,
                model="mrp.bom.line",
            ),
        )
        owner["mapping"] = SimpleNamespace(relationships=(one2many,))
        owner["model"] = SimpleNamespace(
            fields=(
                SimpleNamespace(
                    name="line_ids",
                    label="Bill of Material Lines",
                    type="one2many",
                    relation="mrp.bom.line",
                    relation_field="bom_id",
                ),
            ),
        )
        owner["selected_model"] = "mrp.bom"
        child["selected_model"] = "mrp.bom.line"
        result = RelationshipHealthResult(
            owner_dataset_id="bom-lines",
            target_field="line_ids",
            relationship_kind="one2many",
            resolver_origin=ResolverOrigin.TARGET_CATALOG,
            related_model="mrp.bom.line",
            dependency_dataset_id=None,
            required=False,
            status=RelationshipHealthStatus.NOT_CHECKABLE,
            not_checkable_reason=(
                RelationshipHealthNotCheckableReason.ONE2MANY_INVERSE_REQUIRED
            ),
        )

        view = relationship_health_view(
            workspace_id="workspace:test",
            dataset_views=(owner, child),
            live_check=SimpleNamespace(
                relationship_health_results=(result,),
                schema_changed=False,
            ),
            live_check_current=True,
            check_attempt=None,
            working_draft_is_current=True,
        )

        card = view["cards"][0]
        self.assertIn("child table", card["message"])
        self.assertEqual(
            card["inverse_action"]["label"],
            "Open incoming BOMs -> bom_id",
        )
        self.assertIn("#relationship-1-bom_id", card["inverse_action"]["url"])


def _dataset_views():
    resolver = SimpleNamespace(
        origin=ResolverOrigin.TARGET_THEN_DATASET,
        dataset_id="boms",
        model="mrp.bom",
    )
    relationship = SimpleNamespace(
        target_field="bom_id",
        kind="many2one",
        required=True,
        required_on_create=False,
        resolver=resolver,
    )
    models = (
        SimpleNamespace(name="mrp.bom", label="Bill of Material"),
        SimpleNamespace(name="mrp.bom.line", label="Bill of Material Line"),
    )
    owner = {
        "index": 0,
        "source": SimpleNamespace(dataset_id="bom-lines", name="BOM Lines"),
        "mapping": SimpleNamespace(relationships=(relationship,)),
        "model": SimpleNamespace(
            fields=(
                SimpleNamespace(
                    name="bom_id",
                    label="Parent Bill of Material",
                    type="many2one",
                    relation="mrp.bom",
                    relation_field=None,
                ),
            ),
        ),
        "models": models,
        "selected_model": "mrp.bom.line",
        "odoo_pinned": False,
        "edit_url": "/workspaces/workspace:test/mapping?mapping_dataset=0",
    }
    parent = {
        "index": 1,
        "source": SimpleNamespace(dataset_id="boms", name="incoming BOMs"),
        "mapping": SimpleNamespace(relationships=()),
        "model": SimpleNamespace(fields=()),
        "models": models,
        "selected_model": "mrp.bom",
        "odoo_pinned": False,
        "edit_url": "/workspaces/workspace:test/mapping?mapping_dataset=1",
    }
    return owner, parent


if __name__ == "__main__":
    unittest.main()
