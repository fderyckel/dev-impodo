from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipScope,
    relationship_scope_decisions,
)
from impodo.domain.odoo_source_scope import (
    RelatedDataHandling,
    RelatedDataSuggestion,
)
from impodo.web.presenters.odoo_source_scope import (
    build_related_data_scope_view,
)
from impodo.web.presenters.schema import _schema_model_choices


class OdooSourceScopePresenterTests(unittest.TestCase):
    def test_recommended_models_are_exposed_as_inline_choices(self) -> None:
        view = build_related_data_scope_view(
            "workspace-1",
            (
                _suggestion(
                    "uom_id",
                    "Unit of Measure",
                    "uom.uom",
                    RelatedDataHandling.INCLUDE_SUPPORTING,
                ),
                _suggestion(
                    "categ_id",
                    "Product Category",
                    "product.category",
                    RelatedDataHandling.INCLUDE_SUPPORTING,
                ),
                _suggestion(
                    "bom_ids",
                    "Bills of Materials",
                    "mrp.bom",
                    RelatedDataHandling.SEPARATE_PROCESS,
                ),
                _suggestion(
                    "company_id",
                    "Company",
                    "res.company",
                    RelatedDataHandling.REUSE_DESTINATION,
                ),
            ),
            model_labels={
                "product.category": "Product Categories",
                "uom.uom": "Units of Measure",
                "mrp.bom": "Bills of Materials",
                "res.company": "Companies",
            },
            selected_models=frozenset({"product.template"}),
            available_models=frozenset(
                {"product.category", "uom.uom", "mrp.bom", "res.company"}
            ),
        )

        self.assertEqual(
            tuple(group.handling for group in view.groups),
            (
                RelatedDataHandling.INCLUDE_SUPPORTING,
                RelatedDataHandling.REUSE_DESTINATION,
                RelatedDataHandling.SEPARATE_PROCESS,
            ),
        )
        self.assertEqual(
            view.selectable_model_names,
            ("product.category", "res.company", "uom.uom"),
        )
        self.assertEqual(
            tuple(model.label for model in view.groups[0].models),
            ("Product Categories", "Units of Measure"),
        )
        self.assertFalse(any(model.checked for model in view.groups[0].models))
        self.assertTrue(all(model.recommended for model in view.groups[0].models))
        self.assertTrue(view.groups[1].models[0].can_select)
        self.assertTrue(view.groups[1].models[0].recommended)
        self.assertFalse(view.groups[2].models[0].can_select)

        saved_view = build_related_data_scope_view(
            "workspace-1",
            (
                _suggestion(
                    "categ_id",
                    "Product Category",
                    "product.category",
                    RelatedDataHandling.INCLUDE_SUPPORTING,
                ),
            ),
            model_labels={"product.category": "Product Categories"},
            selected_models=frozenset(
                {"product.template", "product.category"}
            ),
            available_models=frozenset({"product.category"}),
        )
        saved_model = saved_view.groups[0].models[0]
        self.assertTrue(saved_model.checked)
        self.assertFalse(saved_model.recommended)

    def test_schema_review_preselects_only_available_suggestions(self) -> None:
        workspace = SimpleNamespace(
            intended_models=("product.template",),
            intended_applications=("Inventory",),
        )
        catalog = SimpleNamespace(
            models=(
                _model("product.template", "Products"),
                _model("product.category", "Product Categories"),
            )
        )

        choices = _schema_model_choices(
            workspace,
            catalog,
            suggested_models=frozenset(
                {"product.category", "not.available"}
            ),
        )
        by_name = {item["name"]: item for item in choices}

        self.assertTrue(by_name["product.template"]["selected"])
        self.assertFalse(by_name["product.template"]["suggested"])
        self.assertTrue(by_name["product.category"]["selected"])
        self.assertTrue(by_name["product.category"]["suggested"])
        self.assertNotIn("not.available", by_name)

    def test_one_related_model_gets_one_selection_control(self) -> None:
        view = build_related_data_scope_view(
            "workspace-1",
            (
                _suggestion(
                    "uom_id",
                    "Unit of Measure",
                    "uom.uom",
                    RelatedDataHandling.INCLUDE_SUPPORTING,
                ),
                _suggestion(
                    "x_other_uom_id",
                    "Other Unit",
                    "uom.uom",
                    RelatedDataHandling.OPTIONAL_BUSINESS_DATA,
                ),
            ),
            model_labels={"uom.uom": "Units of Measure"},
            available_models=frozenset({"uom.uom"}),
        )

        self.assertEqual(
            sum(
                model.can_select
                for group in view.groups
                for model in group.models
            ),
            1,
        )

    def test_unprofiled_relationship_is_selectable_but_not_recommended(self) -> None:
        view = build_related_data_scope_view(
            "workspace-1",
            (
                _suggestion(
                    "reviewer_id",
                    "Reviewer",
                    "x.reviewer",
                    RelatedDataHandling.NEEDS_DECISION,
                    profiled=False,
                ),
            ),
            model_labels={"x.reviewer": "Reviewers"},
            available_models=frozenset({"x.reviewer"}),
        )

        self.assertEqual(view.groups[0].title, "No standard default")
        self.assertEqual(view.selectable_model_names, ("x.reviewer",))
        self.assertTrue(view.groups[0].models[0].can_select)
        self.assertFalse(view.groups[0].models[0].recommended)

    def test_new_graph_level_is_pending_without_rewriting_saved_decision(self) -> None:
        owner = _suggestion(
            "owner_id",
            "Owner",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
            profiled=False,
        )
        calendar = _suggestion(
            "calendar_id",
            "Calendar",
            "resource.calendar",
            RelatedDataHandling.NEEDS_DECISION,
            profiled=False,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (owner,),
                included_models={"x.owner"},
            ),
            recorded_at=datetime.now(timezone.utc),
            recorded_by="Data manager",
        )

        view = build_related_data_scope_view(
            "workspace-1",
            (owner, calendar),
            model_labels={
                "x.owner": "Owners",
                "resource.calendar": "Working Hours",
            },
            selected_models=scope.included_models,
            available_models=frozenset({"x.owner", "resource.calendar"}),
            relationship_scope=scope,
        )

        by_name = {
            model.name: model
            for group in view.groups
            for model in group.models
        }
        self.assertFalse(view.complete)
        self.assertEqual(view.pending_count, 1)
        self.assertFalse(by_name["x.owner"].pending)
        self.assertTrue(by_name["resource.calendar"].pending)


def _suggestion(
    field_name: str,
    field_label: str,
    relation_model: str,
    handling: RelatedDataHandling,
    *,
    profiled: bool = True,
) -> RelatedDataSuggestion:
    return RelatedDataSuggestion(
        source_model="product.template",
        source_label="Product",
        field_name=field_name,
        field_label=field_label,
        relation_model=relation_model,
        required=False,
        handling=handling,
        recommendation_profile_id=(
            "impodo.standard.odoo.relationships" if profiled else None
        ),
        recommendation_profile_version=1 if profiled else None,
    )


def _model(name: str, label: str):
    return SimpleNamespace(
        name=name,
        label=label,
        modules=("product",),
        state="base",
    )


if __name__ == "__main__":
    unittest.main()
