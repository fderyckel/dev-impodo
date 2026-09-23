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
    def test_recommended_models_are_bound_to_the_schema_review_link(self) -> None:
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
            },
        )

        self.assertEqual(
            tuple(group.handling for group in view.groups),
            (
                RelatedDataHandling.INCLUDE_SUPPORTING,
                RelatedDataHandling.SEPARATE_PROCESS,
            ),
        )
        self.assertEqual(
            view.recommended_review_url,
            "/workspaces/workspace-1/schema?"
            "suggested_model=product.category&suggested_model=uom.uom"
            "#odoo-data-choices",
        )
        self.assertEqual(
            tuple(item.relation_label for item in view.groups[0].items),
            ("Units of Measure", "Product Categories"),
        )

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
