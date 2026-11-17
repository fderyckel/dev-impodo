"""Published Recipes carry field uniqueness into freshly bound source data."""

from datetime import UTC, datetime
from uuid import uuid4
import unittest

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
)
from impodo.domain.workspace.contracts import SchemaModel, SourceSelection
from impodo.web.presenters.recipes import build_recipe_change_view
from tests.domain.recipe.test_representative_shapes import (
    _column,
    _dataset,
    _field,
    _key,
    _publish,
)


class RecipeFieldUniquenessTests(unittest.TestCase):
    def test_publishing_rebinding_and_change_review_preserve_the_rule(self):
        customers = _dataset("Customers", (("ref", "string"), ("vat", "string")), "6")
        selection = SourceSelection(
            str(uuid4()),
            1,
            str(uuid4()),
            datetime.now(UTC),
            "Data manager",
            (customers,),
            "sha256:" + "6" * 64,
        )
        mapping = DatasetMapping(
            dataset_id=customers.dataset_id,
            target_model="res.partner",
            source_identity_column_keys=(_column(customers, "ref"),),
            target_identity=(
                IdentityComponentMapping((_column(customers, "ref"),), ("ref",)),
            ),
            fields=(
                ScalarFieldMapping(
                    "vat", _column(customers, "vat"), must_be_unique=True
                ),
            ),
        )
        recipe = _publish(
            base_selection=selection,
            mapping_selection=selection,
            mappings=(mapping,),
            models=(
                SchemaModel("res.partner", "Customer", (_field("ref"), _field("vat"))),
            ),
            business_keys=(_key("res.partner", "ref"),),
        )
        self.assertEqual(recipe["contract_versions"]["mapping_recipe"], 6)
        dataset = recipe["mapping"]["datasets"][0]
        field = dataset["fields"][0]
        self.assertTrue(field["must_be_unique"])
        logical_source = field["provider"]["source_column_ids"][0]
        rebound = RecipeApplicationCompiler()._field(
            field,
            {logical_source: "fresh.vat"},
            {},
            logical_dataset=dataset["logical_dataset_id"],
        )
        self.assertTrue(rebound.must_be_unique)
        self.assertEqual(rebound.source_column_key, "fresh.vat")
        view = build_recipe_change_view({}, recipe)
        self.assertTrue(view["fields"][0]["must_be_unique"])
