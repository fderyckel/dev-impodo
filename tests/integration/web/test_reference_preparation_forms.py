"""Keep prepared linked matching values editable, portable, and model-neutral."""

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
from starlette.datastructures import FormData

from impodo.domain.mapping.contracts import (
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePreparation,
    prepare_reference_value,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
)
from impodo.web.presenters.mapping_forms import (
    _mapping_allowed_fields,
    _mapping_datasets_from_form,
)
from impodo.web.presenters.mapping_view import _mapping_dataset_views
from impodo.web.presenters.reference_preparation_forms import (
    apply_reference_preparations,
    reference_preparation_editors,
    reference_preparation_preview_row,
)
from tests.support.paths import REPOSITORY_ROOT


class ReferencePreparationFormTests(unittest.TestCase):
    def setUp(self):
        binding = FileSourceBinding(
            "file:refs", "csv", "a" * 64, "sha256:" + "b" * 64, "utf-8", ",", 1
        )
        self.source = SourceDataset(
            "dataset:items",
            "Items",
            binding,
            2,
            (
                SourceDatasetColumn(1, "Site", "site", "string"),
                SourceDatasetColumn(2, "Warehouse", "warehouse", "string"),
                SourceDatasetColumn(3, "Bin", "bin", "string"),
            ),
        )
        self.resolver = RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="x.group",
            key_mappings=(ReferenceKeyMapping("site", "name"),),
        )
        self.prefix = "reference"

    def parse(self, expression, *, bindings=(), source=None):
        return apply_reference_preparations(
            FormData(
                {
                    "reference_0_mode": "formula",
                    "reference_0_formula": expression,
                    "reference_0_bindings": json.dumps(bindings),
                }
            ),
            self.prefix,
            self.resolver,
            source or self.source,
        )

    def test_formula_literals_do_not_become_source_bindings(self):
        resolver = self.parse('concat(value, "/column_2/", column_3)')
        mapping = resolver.key_mappings[0]
        self.assertEqual(mapping.preparation.bindings, (("column_3", "bin"),))
        self.assertEqual(
            prepare_reference_value(mapping, {"site": "A", "bin": "001"}),
            "A/column_2/001",
        )

    def test_malformed_formula_remains_editable(self):
        resolver = self.parse("concat(value, column_2,")
        editor = reference_preparation_editors(resolver, None, self.source, None)[0]
        self.assertEqual(editor["formula"], "concat(value, column_2,")
        self.assertTrue(editor["error"])
        self.assertEqual(editor["mode"], "formula")

    def test_reorder_keeps_old_bindings_and_exposes_new_columns(self):
        saved = self.parse('concat(value, "/", column_2)')
        reordered = replace(
            self.source,
            columns=(
                replace(self.source.columns[2], ordinal=1),
                replace(self.source.columns[0], ordinal=2),
                replace(self.source.columns[1], ordinal=3),
            ),
        )
        editor = reference_preparation_editors(saved, None, reordered, None)[0]
        available = dict(json.loads(editor["bindings_json"]))
        self.assertEqual(available["column_2"], "warehouse")
        self.assertEqual(set(available.values()), {"site", "warehouse", "bin"})
        bin_alias = next(name for name, key in available.items() if key == "bin")
        updated = self.parse(
            f'concat(value, "/", column_2, "/", {bin_alias})',
            bindings=list(available.items()),
            source=reordered,
        )
        self.assertEqual(
            prepare_reference_value(
                updated.key_mappings[0],
                {"site": "A", "warehouse": "0100", "bin": "100"},
            ),
            "A/0100/100",
        )

    def test_combined_parts_keep_order_and_leading_zeroes(self):
        resolver = apply_reference_preparations(
            FormData(
                {
                    "reference_0_mode": "combine",
                    "reference_0_parts": json.dumps(
                        [
                            {"source_column_key": "site"},
                            {"literal": "/Section/"},
                            {"source_column_key": "warehouse"},
                            {"literal": "/"},
                            {"source_column_key": "bin"},
                        ]
                    ),
                }
            ),
            self.prefix,
            self.resolver,
            self.source,
        )
        self.assertEqual(
            prepare_reference_value(
                resolver.key_mappings[0],
                {"site": "A", "warehouse": "0100", "bin": "100"},
            ),
            "A/Section/0100/100",
        )

    def test_preview_uses_one_captured_row_and_retains_missing_cells(self):
        binding = self.source.source
        catalog = SimpleNamespace(
            file_id=binding.file_id,
            source_sha256=binding.source_sha256,
            content_hash=binding.catalog_hash,
            tables=(
                SimpleNamespace(
                    table_key="csv", preview_rows=(("A",), ("B", "0100", "100"))
                ),
            ),
        )
        self.assertEqual(
            reference_preparation_preview_row(self.source, (catalog,)),
            {"site": "A", "warehouse": None, "bin": None},
        )

    def test_form_saves_identity_and_ordinary_linked_preparation_in_both_majors(self):
        def field(name, kind="char", relation=None):
            return SchemaField(name, name, kind, False, False, relation, None, ())

        selection = SimpleNamespace(datasets=(self.source,))
        keys = (
            BusinessKeyDefinition(
                "item",
                "x.item",
                ("code",),
                ("group_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                "group", "x.group", ("name",), status=BusinessKeyStatus.CONFIRMED
            ),
        )
        form = FormData(
            {
                "target_model_0": "x.item",
                "business_key_0": "item",
                "source_identity_0": "site",
                "identity_source_0_0": "site",
                "identity_source_0_1": "site",
                "identity_resolver_key_0_1": "group",
                "identity_origin_0_1": "target_catalog",
                "identity_reference_0_1_0_mode": "formula",
                "identity_reference_0_1_0_formula": 'concat(value, "/", column_2)',
                "relation_value_source_0_1": "source",
                "relation_source_0_1": "site",
                "relation_origin_0_1": "target_catalog",
                "relation_key_0_1": "group",
                "relation_reference_0_1_0_mode": "formula",
                "relation_reference_0_1_0_formula": 'concat(value, "/", column_3)',
            }
        )
        for major in (19, 20):
            with self.subTest(major=major):
                schema = SimpleNamespace(
                    odoo_version=f"{major}.0",
                    models=(
                        SchemaModel(
                            "x.item",
                            "Item",
                            (
                                field("code"),
                                field("group_id", "many2one", "x.group"),
                                field("other_id", "many2one", "x.group"),
                            ),
                        ),
                        SchemaModel("x.group", "Group", (field("name"),)),
                    ),
                )
                governance = SimpleNamespace(business_keys=keys)
                self.assertLessEqual(
                    set(form.keys()), _mapping_allowed_fields(form, selection, schema)
                )
                dataset = _mapping_datasets_from_form(
                    form, selection, schema, governance
                )[0]
                self.assertEqual(
                    dataset.target_scope[0]
                    .resolver.key_mappings[0]
                    .preparation.bindings,
                    (("column_2", "warehouse"),),
                )
                self.assertEqual(
                    dataset.relationships[0]
                    .resolver.key_mappings[0]
                    .preparation.bindings,
                    (("column_3", "bin"),),
                )
                view = _mapping_dataset_views(
                    selection, schema, governance, (dataset,), active_dataset_index=0
                )[0]
                self.assertEqual(
                    view["identity_rows"][1]["reference_preparations"][0]["mode"],
                    "formula",
                )
                self.assertEqual(
                    view["relation_rows"][0]["reference_preparations"][0]["mode"],
                    "formula",
                )

    def test_template_renders_generic_preparation_without_database_access(self):
        mapping = replace(
            self.resolver.key_mappings[0],
            preparation=ReferencePreparation(
                formula='concat(value, "/", column_2)',
                bindings=(("column_2", "warehouse"),),
            ),
        )
        resolver = replace(self.resolver, key_mappings=(mapping,))
        editors = reference_preparation_editors(
            resolver,
            None,
            self.source,
            {"site": "A", "warehouse": "0100", "bin": "100"},
        )
        environment = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )
        html = environment.get_template("mapping/_reference_preparation.html").render(
            row={"reference_preparations": editors},
            view={"source": self.source},
            reference_prefix="reference",
        )
        self.assertIn("Combine columns and fixed text", html)
        self.assertIn("A/0100", html)
        self.assertIn('name="reference_0_formula"', html)
        self.assertIn("Only existing Odoo records", html)
        self.assertIn('data-formula-source-values', html)
        self.assertIn('<code>value</code> (Site)', html)
        self.assertIn('<code>column_2</code> (Warehouse)', html)
        self.assertIn('<code>column_3</code> (Bin)', html)
        self.assertIn('aria-describedby="reference_0_formula_help"', html)
        self.assertNotIn("Source values available to this formula", html)

    def test_unselected_matching_rule_shows_the_next_choice_without_placeholder(self):
        environment = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )
        html = environment.get_template("mapping/_reference_preparation.html").render(
            row={"reference_preparations": reference_preparation_editors(
                None, None, self.source, None,
            )},
            view={"source": self.source},
            reference_prefix="reference",
        )
        self.assertIn("Choose an Odoo matching rule above, then save progress.", html)
        self.assertNotIn("Matching field: matching value", html)
        self.assertIn('<code>column_2</code> (Warehouse)', html)

    def test_named_formula_help_uses_saved_alias_bindings_after_reorder(self):
        saved = self.parse('concat(value, "/", column_2)')
        reordered = replace(self.source, columns=(
            replace(self.source.columns[2], ordinal=1),
            replace(self.source.columns[0], ordinal=2),
            replace(self.source.columns[1], ordinal=3),
        ))
        editor = reference_preparation_editors(saved, None, reordered, None)[0]
        environment = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )
        html = environment.get_template("mapping/_reference_preparation.html").render(
            row={"reference_preparations": (editor,)},
            view={"source": reordered},
            reference_prefix="reference",
        )
        self.assertIn('<code>column_2</code> (Warehouse)', html)
        self.assertNotIn('<code>column_2</code> (Site)', html)

    def test_scalar_formula_help_uses_the_same_visible_column_name_legend(self):
        environment = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )
        html = environment.get_template("mapping/_formula_source_values.html").render(
            formula_value_label="this field", formula_columns=None,
            view={"source": self.source},
        )
        self.assertIn('<code>value</code> (this field)', html)
        self.assertIn('<code>column_1</code> (Site)', html)
        self.assertIn('<code>column_2</code> (Warehouse)', html)
        self.assertIn('<code>column_3</code> (Bin)', html)


if __name__ == "__main__":
    unittest.main()
