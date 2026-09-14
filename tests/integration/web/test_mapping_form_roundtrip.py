"""Reopening checked matches must preserve their semantic content."""

from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader
from starlette.datastructures import FormData

from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import SchemaField, SchemaModel, SourceDataset, SourceDatasetColumn
from impodo.domain.workspace.derived_entities import DerivedDatasetLink
from impodo.web.presenters.mapping_forms import _mapping_datasets_from_form, _merge_partial_mapping_datasets
from impodo.web.presenters.mapping_view import _mapping_dataset_views
from tests.support.paths import REPOSITORY_ROOT


class _RenderedControls(HTMLParser):
    """Collect successful controls from the dataset templates, including hidden ones."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries = []
        self.template_depth = 0
        self.select = None
        self.textarea = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "template":
            self.template_depth += 1
        if self.template_depth:
            return
        if tag == "input" and attrs.get("name") and "disabled" not in attrs:
            kind = attrs.get("type", "text")
            if kind not in {"submit", "button", "checkbox", "radio"} or (
                kind in {"checkbox", "radio"} and "checked" in attrs
            ):
                default = "on" if kind in {"checkbox", "radio"} else ""
                self.entries.append((attrs["name"], attrs.get("value", default)))
        elif tag == "select":
            self.select = (attrs, [])
        elif tag == "option" and self.select is not None:
            self.select[1].append(attrs)
        elif tag == "textarea":
            self.textarea = (attrs, [])

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea[1].append(data)

    def handle_endtag(self, tag):
        if tag == "template":
            self.template_depth -= 1
            return
        if self.template_depth:
            return
        if tag == "select" and self.select is not None:
            attrs, options = self.select
            selected = [option for option in options if "selected" in option]
            if "multiple" not in attrs:
                selected = (selected or options[:1])[-1:]
            if attrs.get("name") and "disabled" not in attrs:
                self.entries.extend(
                    (attrs["name"], option.get("value", "")) for option in selected
                )
            self.select = None
        elif tag == "textarea" and self.textarea is not None:
            attrs, contents = self.textarea
            if attrs.get("name") and "disabled" not in attrs:
                self.entries.append((attrs["name"], "".join(contents)))
            self.textarea = None


class MappingFormRoundTripTests(unittest.TestCase):
    def setUp(self):
        binding = FileSourceBinding(
            file_id="file:products", table_key="csv", source_sha256="a" * 64,
            catalog_hash="sha256:" + "b" * 64, encoding="utf-8",
            delimiter=",", header_row=1,
        )
        self.selection = SimpleNamespace(
            content_hash="sha256:" + "c" * 64,
            datasets=(
                SourceDataset(
                    dataset_id="derived:categories", name="Categories", source=binding,
                    row_count=2,
                    columns=(
                        SourceDatasetColumn(1, "Key", "category.key", "string"),
                        SourceDatasetColumn(2, "Name", "category.name", "string"),
                        SourceDatasetColumn(3, "Parent", "category.parent", "string"),
                    ),
                ),
                SourceDataset(
                    dataset_id="dataset:products", name="Products", source=binding,
                    row_count=2,
                    columns=(SourceDatasetColumn(1, "Code", "product.code", "string"),),
                ),
            ),
        )

        def field(name, kind="char", relation=None):
            return SchemaField(name, name, kind, False, False, relation, None, ())

        self.schema = SimpleNamespace(
            content_hash="sha256:" + "d" * 64,
            models=(
                SchemaModel("product.category", "Category", (
                    field("x_code"), field("name"),
                    field("parent_id", "many2one", "product.category"),
                )),
                SchemaModel("product.template", "Product", (field("default_code"),)),
            ),
        )
        self.governance = SimpleNamespace(
            content_hash="sha256:" + "e" * 64,
            business_keys=tuple(
                BusinessKeyDefinition(
                    key_id=key_id, model=model, key_fields=(target,), scope_fields=(),
                    description=target, status=BusinessKeyStatus.CONFIRMED,
                )
                for key_id, model, target in (
                    ("key:category", "product.category", "x_code"),
                    ("key:product", "product.template", "default_code"),
                )
            ),
        )
        self.links = (DerivedDatasetLink(
            derived_dataset_id="derived:categories", consumer_dataset_id="dataset:products",
            source_column_key="product.code", canonical_key_column_key="category.key",
            name_column_key="category.name", parent_key_column_key="category.parent",
            target_model="product.category", target_name_field="name",
            source_level_column_keys=("product.code",),
        ),)
        self.saved_form = FormData((
            ("target_model_0", "product.category"), ("business_key_0", "key:category"),
            ("source_identity_0", "category.key"), ("identity_source_0_0", "category.key"),
            ("target_model_1", "product.template"), ("business_key_1", "key:product"),
            ("source_identity_1", "product.code"), ("identity_source_1_0", "product.code"),
        ))
        self.definition = canonicalize_mapping_definition(MappingDefinition(
            mapping_id="mapping:test", source_selection_hash=self.selection.content_hash,
            schema_hash=self.governance.content_hash, datasets=self.parse(self.saved_form),
        ))
        environment = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )
        environment.globals["app_icon"] = lambda *args: ""
        self.template = environment.get_template("mapping/_dataset.html")

    def parse(self, form):
        return _mapping_datasets_from_form(
            form, self.selection, self.schema, self.governance, derived_links=self.links,
        )

    def render_form(self, definition, active):
        views = _mapping_dataset_views(
            self.selection, self.schema, self.governance,
            definition.datasets if definition else (), derived_links=self.links,
            active_dataset_index=active, selected_models={
                0: "product.category", 1: "product.template",
            },
        )
        controls = _RenderedControls()
        controls.entries.append((
            "editable_dataset_id", self.selection.datasets[active].dataset_id,
        ))
        for view in views:
            controls.feed(self.template.render(
                view=view, workspace_id="workspace:test", formula_authoring_issues_by_dataset={},
            ))
        controls.close()
        return FormData(controls.entries)

    def roundtrip(self, definition, active):
        form = self.render_form(definition, active)
        datasets = _merge_partial_mapping_datasets(
            self.parse(form), definition, form, self.selection, self.schema,
        )
        return canonicalize_mapping_definition(replace(definition, datasets=datasets))

    def test_reopening_either_table_preserves_checked_matches(self):
        for active in (0, 1):
            with self.subTest(active=active):
                reopened = self.roundtrip(self.definition, active)
                self.assertEqual(reopened.datasets, self.definition.datasets)
                self.assertEqual(reopened.content_hash, self.definition.content_hash)

    def test_new_mapping_still_prefills_generated_name_and_parent(self):
        category = self.parse(self.render_form(None, 0))[0]
        self.assertEqual(category.fields[0].target_field, "name")
        self.assertEqual(category.fields[0].source_column_key, "category.name")
        self.assertEqual(category.relationships[0].target_field, "parent_id")
        self.assertEqual(category.relationships[0].source_column_keys, ("category.parent",))

    def test_saved_generated_name_and_parent_are_preserved(self):
        form = FormData((
            *self.saved_form.multi_items(),
            ("scalar_value_source_0_1", "source"), ("scalar_source_0_1", "category.name"),
            ("relation_value_source_0_0", "source"), ("relation_source_0_0", "category.parent"),
            ("relation_origin_0_0", "dataset"), ("relation_dataset_0_0", "derived:categories"),
        ))
        definition = canonicalize_mapping_definition(replace(self.definition, datasets=self.parse(form)))
        self.assertEqual(self.roundtrip(definition, 0), definition)


if __name__ == "__main__":
    unittest.main()
