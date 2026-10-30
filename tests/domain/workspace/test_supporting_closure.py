"""Reviewed business intent survives Recipe rebinding and rejects contradictions."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from impodo.domain.mapping.contracts import (
    DatasetMapping, ReferenceKeyMapping, RelationshipMapping, RelationshipResolver,
    ResolverOrigin, TargetFieldDisposition, TargetFieldHandling,
)
from impodo.domain.workspace.supporting_closure import (
    close_supporting_plan, portable_supporting_choices, recipe_supporting_choices,
    supporting_mapping_conflicts,
)
from impodo.domain.workspace.supporting_review import SupportingChoice
from tests.domain.workspace.test_supporting_plan import model, relation, dataset


def relationship(field, related, *, incoming=None):
    return RelationshipMapping(
        field, "many2one", ("source",), RelationshipResolver(
            ResolverOrigin.TARGET_THEN_DATASET if incoming else ResolverOrigin.TARGET_CATALOG,
            dataset_id=incoming, model=related,
            key_mappings=(ReferenceKeyMapping("source", "name"),),
        ),
    )


class SupportingClosureTests(unittest.TestCase):
    def setUp(self):
        self.schema = SimpleNamespace(content_hash="schema", models=(
            model("product.template", relation("categ_id", "product.category")),
            model("mrp.bom", relation("product_tmpl_id", "product.template")),
        ))
        self.choices = (
            SupportingChoice("product.template", "categ_id", "REUSE", key_fields=("name",)),
            SupportingChoice("mrp.bom", "product_tmpl_id", "INCOMING", "products"),
        )
        self.mappings = (
            DatasetMapping("products", "product.template", relationships=(relationship("categ_id", "product.category"),)),
            DatasetMapping("boms", "mrp.bom", relationships=(relationship("product_tmpl_id", "product.template", incoming="products"),)),
        )

    def test_products_and_boms_keep_reuse_and_target_then_incoming_meaning(self):
        self.assertEqual(supporting_mapping_conflicts(self.choices, self.mappings, self.schema), ())
        closed = close_supporting_plan(
            schema=self.schema, datasets=(dataset("products"), dataset("boms")),
            mappings=self.mappings, choices=self.choices, source_hash="source",
            preparation_hash="rules", recipe_hash="selected-recipe",
        )
        self.assertTrue(closed.proposal.complete)
        edges = {(item.source_model, item.field_name): item for item in closed.proposal.edges}
        self.assertEqual(edges[("mrp.bom", "product_tmpl_id")].candidates[0].dataset_id, "products")
        self.assertEqual(edges[("product.template", "categ_id")].role.value, "REUSE_EXISTING")
        self.assertNotEqual(closed.content_hash, replace(closed, recipe_hash="changed-recipe").content_hash)

    def test_incoming_choice_cannot_become_target_only_or_another_table(self):
        for resolver in (
            RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="product.template", key_mappings=(ReferenceKeyMapping("source", "name"),)),
            replace(self.mappings[1].relationships[0].resolver, dataset_id="another"),
        ):
            changed = replace(self.mappings[1], relationships=(replace(self.mappings[1].relationships[0], resolver=resolver),))
            conflicts = supporting_mapping_conflicts(self.choices, (self.mappings[0], changed), self.schema)
            self.assertEqual(len(conflicts), 1)
            self.assertIn("mrp.bom.product_tmpl_id", conflicts[0])

    def test_reuse_cannot_gain_incoming_fallback_or_wider_key(self):
        original = self.mappings[0].relationships[0]
        for resolver in (
            replace(original.resolver, origin=ResolverOrigin.TARGET_THEN_DATASET, dataset_id="categories"),
            replace(original.resolver, key_mappings=(ReferenceKeyMapping("source", "code"),)),
        ):
            changed = replace(self.mappings[0], relationships=(replace(original, resolver=resolver),))
            self.assertIn("product.template.categ_id", supporting_mapping_conflicts(self.choices, (changed, self.mappings[1]), self.schema)[0])

    def test_default_managed_and_unused_choices_do_not_supply_a_relationship(self):
        for flags, action, handling in (
            ({"create_default_present": True}, "CHECKED_DEFAULT", TargetFieldHandling.ODOO_DEFAULT),
            ({"computed": True}, "ODOO_MANAGED", TargetFieldHandling.ODOO_MANAGED),
            ({}, "SKIP_OPTIONAL", None),
        ):
            schema = SimpleNamespace(models=(model("product.template", relation("categ_id", "product.category", required=False, **flags)),))
            choice = (SupportingChoice("product.template", "categ_id", action),)
            supplied = (replace(self.mappings[0], target_field_dispositions=()),)
            self.assertTrue(supporting_mapping_conflicts(choice, supplied, schema))
            omitted = (DatasetMapping("products", "product.template", target_field_dispositions=(TargetFieldDisposition("categ_id", handling),) if handling else ()),)
            self.assertEqual(supporting_mapping_conflicts(choice, omitted, schema), ())

    def test_portable_choices_rebind_incoming_data_without_physical_identity(self):
        encoded = portable_supporting_choices(self.choices, {"products": "dataset:products"})
        rebound = recipe_supporting_choices({"supporting_choices": encoded}, {"dataset:products": "fresh-products"})
        incoming = next(item for item in rebound if item.dataset_id)
        self.assertEqual(incoming.dataset_id, "fresh-products")
        self.assertEqual(next(item for item in rebound if item.action.value == "REUSE").key_fields, ("name",))
        with self.assertRaisesRegex(ValueError, "Bind"):
            recipe_supporting_choices({"supporting_choices": encoded}, {})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            recipe_supporting_choices({"supporting_choices": [encoded[0], encoded[0]]})

    def test_skipping_inverse_children_cannot_hide_incoming_child_writes(self):
        child = replace(relation("line_ids", "mrp.bom.line", required=False, kind="one2many"), relation_field="bom_id")
        schema = SimpleNamespace(models=(model("mrp.bom", child), model("mrp.bom.line", relation("bom_id", "mrp.bom"))))
        mappings = (
            DatasetMapping("boms", "mrp.bom"),
            DatasetMapping("lines", "mrp.bom.line", relationships=(relationship("bom_id", "mrp.bom", incoming="boms"),)),
        )
        conflicts = supporting_mapping_conflicts((SupportingChoice("mrp.bom", "line_ids", "SKIP_OPTIONAL"),), mappings, schema)
        self.assertIn("mrp.bom.line_ids", conflicts[0])

    def test_closure_only_carries_choices_for_the_actual_mapping_models(self):
        unused = SupportingChoice("x.other", "country_id", "REUSE", key_fields=("code",))
        schema = SimpleNamespace(content_hash="schema", models=(*self.schema.models, model("x.other", relation("country_id", "res.country"))))
        closed = close_supporting_plan(schema=schema, datasets=(dataset("products"), dataset("boms")), mappings=self.mappings, choices=(*self.choices, unused), source_hash="source", preparation_hash=None)
        self.assertTrue(closed.proposal.complete)
        self.assertNotIn(unused, closed.choices)
