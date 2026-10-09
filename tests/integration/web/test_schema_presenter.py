from __future__ import annotations

from types import SimpleNamespace
import unittest

from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from impodo.domain.workspace.contracts import (
    OdooModelSummary,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.web.presenters.schema import _schema_key_views
from impodo.web.presenters.supporting_models import supporting_model_plan_view


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

    def test_supporting_plan_previews_minimum_preservation_without_root_link(
        self,
    ) -> None:
        schema = SimpleNamespace(
            origin=SchemaOrigin.LIVE_API,
            content_hash="sha256:" + "b" * 64,
            models=(
                _model(
                    "product.template",
                    "Products",
                    (
                        _field(
                            "categ_id",
                            "Category",
                            "many2one",
                            relation="product.category",
                            required=True,
                        ),
                        _field(
                            "uom_id",
                            "Unit of Measure",
                            "many2one",
                            relation="uom.uom",
                            required=True,
                        ),
                        _field(
                            "x_owner_id",
                            "Owner",
                            "many2one",
                            relation="x.owner",
                            required=True,
                        ),
                    ),
                ),
            ),
        )
        catalog = SimpleNamespace(
            models=(
                OdooModelSummary(
                    name="product.category",
                    label="Product Categories",
                    modules=("product",),
                    state="base",
                ),
                OdooModelSummary(
                    name="uom.uom",
                    label="Units of Measure",
                    modules=("uom",),
                    state="base",
                ),
            )
        )

        view = supporting_model_plan_view("workspace-1", schema, catalog)

        self.assertEqual(view["dependency_count"], 3)
        keep_linked = next(
            group
            for group in view["groups"]
            if group["outcome"].value == "KEEP_LINKED"
        )
        self.assertEqual(keep_linked["title"], "Keep linked value")
        self.assertEqual(
            keep_linked["status_label"],
            "Reuse or create identity and required values only",
        )
        by_model = {
            item["model_name"]: item for item in keep_linked["models"]
        }
        self.assertIn("product.category", by_model)
        self.assertIn("uom.uom", by_model)
        self.assertNotIn("include_url", by_model["product.category"])
        self.assertEqual(view["suggestable_model_names"], ())
        issue_summary = view["issue_summary"]
        self.assertEqual(issue_summary.must_fix_count, 1)
        self.assertIn("x.owner", issue_summary.issues[0].code)

    def test_destination_setup_is_existing_only_even_when_odoo_has_a_default(
        self,
    ) -> None:
        schema = SimpleNamespace(
            origin=SchemaOrigin.LIVE_API,
            content_hash="sha256:" + "c" * 64,
            models=(
                _model(
                    "x.asset",
                    "Assets",
                    (
                        _field(
                            "company_id",
                            "Company",
                            "many2one",
                            relation="res.company",
                            required=True,
                            create_default_present=True,
                        ),
                    ),
                ),
            ),
        )

        view = supporting_model_plan_view(
            "workspace-1",
            schema,
            SimpleNamespace(models=()),
        )

        existing_only = next(
            group
            for group in view["groups"]
            if group["outcome"].value == "USE_EXISTING_DESTINATION"
        )
        self.assertEqual(
            existing_only["title"],
            "Use existing destination records only",
        )
        self.assertTrue(
            existing_only["models"][0]["availability_relevant"]
        )
        self.assertEqual(view["issue_summary"].must_fix_count, 1)
        self.assertIn("res.company", view["issue_summary"].issues[0].code)


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
    required: bool = False,
    create_default_present: bool = False,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        readonly=False,
        required=required,
        relation=relation,
        relation_field=None,
        selection=(),
        create_default_present=create_default_present,
    )


if __name__ == "__main__":
    unittest.main()
