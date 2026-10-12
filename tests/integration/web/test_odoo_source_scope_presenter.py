from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
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
            ("mrp.bom", "product.category", "res.company", "uom.uom"),
        )
        self.assertEqual(
            tuple(model.label for model in view.groups[0].models),
            ("Product Categories", "Units of Measure"),
        )
        self.assertTrue(all(
            model.action is OdooRelationshipCaptureAction.PRESERVE_LINKED
            for model in view.groups[0].models
        ))
        self.assertTrue(all(
            model.recommended_action
            is OdooRelationshipCaptureAction.PRESERVE_LINKED
            for model in view.groups[0].models
        ))
        self.assertEqual(
            view.groups[0].models[0].action_title,
            "Keep linked value",
        )
        self.assertIn(
            "prepare only its identity and required values",
            view.groups[0].models[0].action_description,
        )
        self.assertIn(
            "asks for a decision before any load",
            view.groups[0].models[0].action_description,
        )
        self.assertTrue(all(model.recommended for model in view.groups[0].models))
        self.assertTrue(view.groups[1].models[0].can_select)
        self.assertTrue(view.groups[1].models[0].recommended)
        self.assertIs(
            view.groups[1].models[0].action,
            OdooRelationshipCaptureAction.MATCH_EXISTING,
        )
        self.assertTrue(view.groups[2].models[0].can_select)
        self.assertIs(
            view.groups[2].models[0].action,
            OdooRelationshipCaptureAction.MATCH_EXISTING,
        )

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
        self.assertIs(
            saved_model.action,
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
        )
        self.assertEqual(
            saved_model.action_title,
            "Transfer related records as migration data",
        )
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

    def test_schema_review_keeps_automatic_support_out_of_main_choices(self) -> None:
        workspace = SimpleNamespace(
            intended_models=("mrp.bom.line", "product.product"),
            intended_applications=("Manufacturing",),
        )
        catalog = SimpleNamespace(
            models=(
                _model("mrp.bom.line", "Bill of Material Lines"),
                _model("product.product", "Product Variants"),
            )
        )

        choices = _schema_model_choices(
            workspace,
            catalog,
            selected_models=frozenset({"mrp.bom.line"}),
            automatic_supporting_models=frozenset({"product.product"}),
        )
        by_name = {item["name"]: item for item in choices}

        self.assertTrue(by_name["mrp.bom.line"]["selected"])
        self.assertFalse(by_name["mrp.bom.line"]["automatic_supporting"])
        self.assertFalse(by_name["product.product"]["selected"])
        self.assertTrue(by_name["product.product"]["automatic_supporting"])

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

    def test_incompatible_model_defaults_become_one_named_exception(self) -> None:
        view = build_related_data_scope_view(
            "workspace-1",
            (
                _suggestion(
                    "owner_id",
                    "Owner",
                    "x.shared",
                    RelatedDataHandling.NEEDS_DECISION,
                ),
                _suggestion(
                    "shared_ids",
                    "Shared rows",
                    "x.shared",
                    RelatedDataHandling.NEEDS_DECISION,
                    relationship_type="one2many",
                ),
            ),
            model_labels={"x.shared": "Shared records"},
            available_models=frozenset({"x.shared"}),
        )

        self.assertEqual(view.decision_required_count, 1)
        self.assertEqual(view.conflicting_model_names, ("x.shared",))
        self.assertEqual(
            tuple(model.name for model in view.exception_models),
            ("x.shared",),
        )
        self.assertTrue(view.exception_models[0].preserve_allowed)
        self.assertEqual(
            tuple(
                (item.field_label, item.recommended_action_title)
                for item in view.exception_models[0].items
            ),
            (
                ("Owner", "Keep linked value"),
                ("Shared rows", "Do not include"),
            ),
        )

    def test_unprofiled_relationship_gets_safe_default_without_profile_badge(
        self,
    ) -> None:
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

        self.assertEqual(
            view.groups[0].title,
            "Safe default for an unclassified link",
        )
        self.assertIn(
            "cannot infer the full business scope",
            view.groups[0].description,
        )
        self.assertEqual(view.selectable_model_names, ("x.reviewer",))
        self.assertTrue(view.groups[0].models[0].can_select)
        self.assertFalse(view.groups[0].models[0].recommended)
        self.assertIs(
            view.groups[0].models[0].action,
            OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )

    def test_saved_preserve_leaf_stays_recommended_after_it_joins_scope(self) -> None:
        category = _suggestion(
            "categ_id",
            "Product Category",
            "product.category",
            RelatedDataHandling.INCLUDE_SUPPORTING,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (category,),
                actions_by_model={
                    "product.category": (
                        OdooRelationshipCaptureAction.PRESERVE_LINKED
                    )
                },
            ),
            root_models=("product.template",),
            recorded_at=datetime.now(timezone.utc),
            recorded_by="Data manager",
        )

        view = build_related_data_scope_view(
            "workspace-1",
            (category,),
            model_labels={"product.category": "Product Categories"},
            selected_models=frozenset(
                {"product.template", "product.category"}
            ),
            available_models=frozenset({"product.category"}),
            relationship_scope=scope,
        )

        model = view.groups[0].models[0]
        self.assertIs(
            model.action,
            OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )
        self.assertIs(model.action, model.recommended_action)

    def test_deliberate_linked_data_choice_is_visible_after_refresh(self) -> None:
        category = _suggestion(
            "categ_id",
            "Product Category",
            "product.category",
            RelatedDataHandling.INCLUDE_SUPPORTING,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (category,),
                actions_by_model={
                    "product.category": (
                        OdooRelationshipCaptureAction.MATCH_EXISTING
                    )
                },
            ),
            root_models=("product.template",),
            explicit_actions_by_model=(
                (
                    "product.category",
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                ),
            ),
            recorded_at=datetime.now(timezone.utc),
            recorded_by="Data manager",
        )

        view = build_related_data_scope_view(
            "workspace-1",
            (category,),
            model_labels={"product.category": "Product Categories"},
            selected_models=frozenset(
                {"product.template", "product.category"}
            ),
            available_models=frozenset({"product.category"}),
            relationship_scope=scope,
        )

        self.assertEqual(len(view.saved_choice_models), 1)
        saved = view.saved_choice_models[0]
        self.assertEqual(saved.label, "Product Categories")
        self.assertEqual(
            saved.action_title,
            "Use existing destination records only",
        )
        self.assertTrue(saved.saved_choice)

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
            explicit_actions_by_model=(
                (
                    "x.owner",
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                ),
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
    relationship_type: str = "many2one",
) -> RelatedDataSuggestion:
    return RelatedDataSuggestion(
        source_model="product.template",
        source_label="Product",
        field_name=field_name,
        field_label=field_label,
        relation_model=relation_model,
        relationship_type=relationship_type,
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
