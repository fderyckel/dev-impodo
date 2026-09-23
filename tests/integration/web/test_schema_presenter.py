from __future__ import annotations

from types import SimpleNamespace
import unittest

from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.web.presenters.schema import _schema_key_views


class SchemaMatchingRulePresenterTests(unittest.TestCase):
    def test_safe_recommendation_prefills_server_rendered_form_values(self) -> None:
        views = _schema_key_views(
            SimpleNamespace(
                models=(
                    _model(
                        "mrp.routing.workcenter",
                        "Work Center Usage",
                        (
                            _field("name", "Operation", "char"),
                            _field(
                                "bom_id",
                                "Bill of Material",
                                "many2one",
                                relation="mrp.bom",
                            ),
                        ),
                    ),
                )
            ),
            {},
        )

        self.assertEqual(views[0]["key_fields"], ("name",))
        self.assertEqual(views[0]["scope_fields"], ("bom_id",))
        self.assertTrue(views[0]["is_prefilled"])
        self.assertEqual(
            views[0]["selection_summary"],
            "Operation (name), within Bill of Material (bom_id)",
        )

    def test_confirmed_rule_wins_over_a_new_policy_suggestion(self) -> None:
        existing = BusinessKeyDefinition(
            key_id="bom-component",
            model="mrp.bom.line",
            key_fields=("product_id",),
            scope_fields=("bom_id",),
            status=BusinessKeyStatus.CONFIRMED,
        )
        views = _schema_key_views(
            SimpleNamespace(
                models=(
                    _model(
                        "mrp.bom.line",
                        "Bill of Material Line",
                        (
                            _field("sequence", "Sequence", "integer"),
                            _field(
                                "bom_id",
                                "Parent BoM",
                                "many2one",
                                relation="mrp.bom",
                            ),
                            _field(
                                "product_id",
                                "Component",
                                "many2one",
                                relation="product.product",
                            ),
                        ),
                    ),
                )
            ),
            {existing.model: existing},
        )

        self.assertEqual(views[0]["key_fields"], ("product_id",))
        self.assertEqual(views[0]["scope_fields"], ("bom_id",))
        self.assertFalse(views[0]["is_prefilled"])

    def test_unsafe_model_remains_unresolved(self) -> None:
        views = _schema_key_views(
            SimpleNamespace(
                models=(
                    _model(
                        "x.ambiguous",
                        "Ambiguous",
                        (_field("name", "Name", "char"),),
                    ),
                )
            ),
            {},
        )

        self.assertEqual(views[0]["key_fields"], ())
        self.assertFalse(views[0]["is_prefilled"])

    def test_submitted_advanced_override_is_kept_and_marked_changed(self) -> None:
        views = _schema_key_views(
            SimpleNamespace(
                models=(
                    _model(
                        "mrp.bom.line",
                        "Bill of Material Line",
                        (
                            _field("sequence", "Sequence", "integer"),
                            _field(
                                "bom_id",
                                "Parent BoM",
                                "many2one",
                                relation="mrp.bom",
                            ),
                            _field(
                                "product_id",
                                "Component",
                                "many2one",
                                relation="product.product",
                            ),
                        ),
                    ),
                )
            ),
            {},
            key_drafts={
                "mrp.bom.line": (
                    ("product_id",),
                    ("bom_id",),
                    "Component within parent BoM",
                )
            },
        )

        self.assertEqual(views[0]["key_fields"], ("product_id",))
        self.assertTrue(views[0]["has_submitted_draft"])
        self.assertTrue(views[0]["is_changed_from_suggestion"])


def _model(
    name: str,
    label: str,
    fields: tuple[SchemaField, ...],
) -> SchemaModel:
    return SchemaModel(name=name, label=label, fields=fields)


def _field(
    name: str,
    label: str,
    field_type: str,
    *,
    relation: str | None = None,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        readonly=False,
        required=False,
        relation=relation,
        relation_field=None,
        selection=(),
    )


if __name__ == "__main__":
    unittest.main()
