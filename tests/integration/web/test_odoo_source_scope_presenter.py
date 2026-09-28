from __future__ import annotations

from types import SimpleNamespace
import unittest

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
            ),
            model_labels={
                "product.category": "Product Categories",
                "uom.uom": "Units of Measure",
                "mrp.bom": "Bills of Materials",
            },
            selected_models=frozenset({"product.template"}),
            available_models=frozenset(
                {"product.category", "uom.uom", "mrp.bom"}
            ),
        )

        self.assertEqual(
            tuple(group.handling for group in view.groups),
            (
                RelatedDataHandling.INCLUDE_SUPPORTING,
                RelatedDataHandling.SEPARATE_PROCESS,
            ),
        )
        self.assertEqual(
            view.selectable_model_names,
            ("product.category", "uom.uom"),
        )
        self.assertEqual(
            tuple(model.label for model in view.groups[0].models),
            ("Product Categories", "Units of Measure"),
        )
        self.assertFalse(any(model.checked for model in view.groups[0].models))
        self.assertTrue(all(model.recommended for model in view.groups[0].models))
        self.assertFalse(view.groups[1].models[0].can_select)

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


def _suggestion(
    field_name: str,
    field_label: str,
    relation_model: str,
    handling: RelatedDataHandling,
) -> RelatedDataSuggestion:
    return RelatedDataSuggestion(
        source_model="product.template",
        source_label="Product",
        field_name=field_name,
        field_label=field_label,
        relation_model=relation_model,
        required=False,
        handling=handling,
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
