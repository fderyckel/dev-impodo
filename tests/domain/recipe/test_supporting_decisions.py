"""Compile reviewed supporting meaning without carrying physical input identities."""

from datetime import UTC, datetime
from dataclasses import replace
import unittest
from uuid import uuid4

from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping, ReferenceKeyMapping, RelationshipMapping, RelationshipResolver, ResolverOrigin
from impodo.domain.workspace.contracts import SchemaModel, SourceSelection
from impodo.domain.workspace.supporting_review import SupportingChoice
from impodo.domain.workspace.supporting_closure import recipe_supporting_choices
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.domain.recipe_applications import RecipeControlValues
from impodo.domain.shared.access import ActorIdentity
from tests.domain.recipe.test_representative_shapes import _dataset, _field, _key, _column, _publish


class RecipeSupportingDecisionTests(unittest.TestCase):
    def test_unused_relationship_stays_in_the_next_target_metadata_contract(self):
        products = _dataset("Products", (("reference", "string"),), "a")
        selection = SourceSelection(str(uuid4()), 1, str(uuid4()), datetime.now(UTC), "Manager", (products,), "sha256:" + "d" * 64)
        mapping = DatasetMapping(products.dataset_id, "product.template", source_identity_column_keys=(_column(products, "reference"),), target_identity=(IdentityComponentMapping((_column(products, "reference"),), ("default_code",)),))
        recipe = _publish(base_selection=selection, mapping_selection=selection, mappings=(mapping,), models=(SchemaModel("product.template", "Products", (_field("default_code"), _field("categ_id", "many2one", relation="product.category"))),), business_keys=(_key("product.template", "default_code"),), supporting_choices=(SupportingChoice("product.template", "categ_id", "SKIP_OPTIONAL"),))
        target = recipe["odoo_target_contract"]
        self.assertEqual({item["model"] for item in target["models"]}, {"product.template"})
        self.assertIn("categ_id", {item["name"] for item in target["models"][0]["fields"]})
        self.assertEqual(target["models"][0]["reference_paths"], [])

    def test_publication_uses_logical_incoming_identity_and_rebinds_next_delivery(self):
        products = _dataset("Products", (("reference", "string"),), "a")
        boms = _dataset("BOMs", (("reference", "string"), ("product", "string")), "b")
        selection = SourceSelection(str(uuid4()), 1, str(uuid4()), datetime.now(UTC), "Manager", (products, boms), "sha256:" + "d" * 64)
        relation = RelationshipMapping(
            "product_tmpl_id", "many2one", (_column(boms, "product"),),
            RelationshipResolver(ResolverOrigin.TARGET_THEN_DATASET, dataset_id=products.dataset_id, model="product.template", key_mappings=(ReferenceKeyMapping(_column(boms, "product"), "default_code"),)),
        )
        mappings = (
            DatasetMapping(products.dataset_id, "product.template", source_identity_column_keys=(_column(products, "reference"),), target_identity=(IdentityComponentMapping((_column(products, "reference"),), ("default_code",)),)),
            DatasetMapping(boms.dataset_id, "mrp.bom", source_identity_column_keys=(_column(boms, "reference"),), target_identity=(IdentityComponentMapping((_column(boms, "reference"),), ("code",)),), relationships=(relation,)),
        )
        recipe = _publish(
            base_selection=selection, mapping_selection=selection, mappings=mappings,
            models=(SchemaModel("product.template", "Products", (_field("default_code"),)), SchemaModel("mrp.bom", "BOMs", (_field("code"), _field("product_tmpl_id", "many2one", relation="product.template")))),
            business_keys=(_key("product.template", "default_code"), _key("mrp.bom", "code")),
            supporting_choices=(SupportingChoice("mrp.bom", "product_tmpl_id", "INCOMING", products.dataset_id),),
        )
        contract = recipe["odoo_target_contract"]
        portable = recipe_supporting_choices(contract)
        self.assertTrue(portable[0].dataset_id.startswith("dataset:"))
        self.assertNotIn(products.dataset_id, str(contract))
        rebound = recipe_supporting_choices(contract, {portable[0].dataset_id: "next-delivery-products"})
        self.assertEqual(rebound[0].dataset_id, "next-delivery-products")
        fresh = replace(selection, data_version_id=str(uuid4()), datasets=(replace(products, dataset_id="next-delivery-products"), replace(boms, dataset_id="next-delivery-boms")))
        _datasets, dataset_ids, columns = RecipeCompiler._source_identity(fresh.datasets)
        bindings = {logical: physical for physical, logical in dataset_ids.items()}
        bindings.update({logical: physical[1] for physical, logical in columns.items()})
        applied = RecipeApplicationCompiler()._mapping_datasets(
            recipe, bindings, fresh,
            RecipeControlValues(fresh.data_version_id, {}, ActorIdentity("local", "manager", "Manager"), datetime.now(UTC)), None,
        )
        actual = next(item for item in applied if item.target_model == "mrp.bom")
        self.assertEqual(actual.relationships[0].resolver.dataset_id, "next-delivery-products")
        self.assertEqual(actual.relationships[0].resolver.origin, ResolverOrigin.TARGET_THEN_DATASET)
