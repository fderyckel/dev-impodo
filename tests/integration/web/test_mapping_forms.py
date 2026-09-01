from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

import json
import unittest
from types import SimpleNamespace

from starlette.datastructures import FormData

from impodo.web.presenters.mapping_forms import (
    _mapping_allowed_fields,
    _mapping_datasets_from_form,
    _text_steps_from_form,
)
from impodo.web.presenters.mapping_view import _is_phone_field
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
)


class OrderedTextStepFormTests(unittest.TestCase):
    def test_generated_record_link_is_preserved_by_the_mapping_form(self) -> None:
        file_binding = FileSourceBinding(
            file_id="file:test",
            table_key="csv",
            source_sha256="a" * 64,
            catalog_hash="sha256:" + "b" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        )
        selection = SimpleNamespace(
            datasets=(
                SourceDataset(
                    dataset_id="dataset:products",
                    name="Products",
                    source=file_binding,
                    row_count=2,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "default_code",
                            "product.default_code",
                            "string",
                        ),
                    ),
                ),
                SourceDataset(
                    dataset_id="dataset:bom-lines",
                    name="BOM lines",
                    source=file_binding,
                    row_count=1,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "component_code",
                            "line.component_code",
                            "string",
                        ),
                    ),
                ),
            )
        )

        def field(
            name: str,
            field_type: str,
            *,
            readonly: bool = False,
            relation: str | None = None,
        ) -> SchemaField:
            return SchemaField(
                name=name,
                label=name.replace("_", " ").title(),
                type=field_type,
                required=False,
                readonly=readonly,
                relation=relation,
                relation_field=None,
                selection=(),
            )

        schema = SimpleNamespace(
            models=(
                SchemaModel(
                    "product.template",
                    "Product",
                    (
                        field("default_code", "char"),
                        field(
                            "product_variant_id",
                            "many2one",
                            readonly=True,
                            relation="product.product",
                        ),
                    ),
                ),
                SchemaModel(
                    "mrp.bom.line",
                    "BOM line",
                    (
                        field(
                            "product_id",
                            "many2one",
                            relation="product.product",
                        ),
                    ),
                ),
                SchemaModel(
                    "product.product",
                    "Product variant",
                    (field("default_code", "char"),),
                ),
            )
        )
        product_key = BusinessKeyDefinition(
            key_id="key:product-product-code",
            model="product.product",
            key_fields=("default_code",),
            scope_fields=(),
            description="Product variant code",
            status=BusinessKeyStatus.CONFIRMED,
        )
        form = FormData(
            (
                ("target_model_0", "product.template"),
                ("target_model_1", "mrp.bom.line"),
                ("relation_source_1_0", "line.component_code"),
                ("relation_origin_1_0", "target_then_dataset"),
                ("relation_dataset_1_0", "dataset:products"),
                (
                    "relation_projection_1_0",
                    "dataset:products|product_variant_id",
                ),
                ("relation_key_1_0", product_key.key_id),
            )
        )

        datasets = _mapping_datasets_from_form(
            form,
            selection,
            schema,
            governance=SimpleNamespace(business_keys=(product_key,)),
        )

        resolver = datasets[1].relationships[0].resolver
        self.assertEqual(resolver.dataset_id, "dataset:products")
        self.assertEqual(
            resolver.dataset_projection_field,
            "product_variant_id",
        )

    def test_phone_quick_start_is_suggested_only_for_phone_fields(self) -> None:
        for name, label in (
            ("phone", "Phone"),
            ("mobile", "Mobile"),
            ("x_studio_telephone", "Telephone"),
            ("x_contact", "Téléphone principal"),
        ):
            with self.subTest(name=name, label=label):
                self.assertTrue(
                    _is_phone_field(
                        SimpleNamespace(name=name, label=label, type="char")
                    )
                )
        self.assertFalse(
            _is_phone_field(
                SimpleNamespace(name="email", label="Email", type="char")
            )
        )
        self.assertFalse(
            _is_phone_field(
                SimpleNamespace(name="phone", label="Phone", type="integer")
            )
        )

    def test_reads_ordered_steps_without_interpreting_business_text(self) -> None:
        payload = [
            {
                "kind": "find_replace",
                "search_value": "00",
                "replacement_value": "+",
                "search_mode": "starts_with",
                "replace_all": False,
                "characters": "",
            },
            {
                "kind": "remove_separators_between_digits",
                "search_value": "",
                "replacement_value": "",
                "search_mode": "literal",
                "replace_all": True,
                "characters": " .-",
            },
        ]
        form = FormData((("steps", json.dumps(payload)),))

        steps = _text_steps_from_form(form, "steps")

        assert steps is not None
        self.assertEqual(
            [(item.kind, item.search_mode) for item in steps],
            [
                ("find_replace", "starts_with"),
                ("remove_separators_between_digits", "literal"),
            ],
        )
        self.assertEqual(steps[1].characters, " .-")

    def test_rejects_tampered_or_oversized_step_payloads(self) -> None:
        valid = {
            "kind": "find_replace",
            "search_value": "a",
            "replacement_value": "b",
            "search_mode": "literal",
            "replace_all": True,
            "characters": "",
        }
        tampered = FormData(
            (("steps", json.dumps([{**valid, "unexpected": "value"}])),)
        )
        oversized = FormData(
            (("steps", json.dumps([valid for _index in range(21)])),)
        )

        with self.assertRaisesRegex(ValueError, "invalid"):
            _text_steps_from_form(tampered, "steps")
        with self.assertRaisesRegex(ValueError, "invalid"):
            _text_steps_from_form(oversized, "steps")
        self.assertEqual(_text_steps_from_form(FormData(), "steps"), ())

    def test_retired_single_rule_form_names_are_not_allowed(self) -> None:
        selection = SimpleNamespace(datasets=(SimpleNamespace(),))
        schema = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="res.partner",
                    fields=(SimpleNamespace(type="char"),),
                ),
            )
        )
        form = FormData((("target_model_0", "res.partner"),))

        allowed = _mapping_allowed_fields(form, selection, schema)

        self.assertIn("scalar_text_steps_0_0", allowed)
        self.assertNotIn("scalar_search_0_0", allowed)
        self.assertNotIn("scalar_replacement_0_0", allowed)
        self.assertNotIn("scalar_search_mode_0_0", allowed)
        self.assertNotIn("scalar_replace_all_0_0", allowed)

    def test_button_authored_text_steps_notify_mapping_draft_tracking(self) -> None:
        script = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "web"
            / "static"
            / "mapping-value-rules.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'storage?.dispatchEvent(new Event("input", { bubbles: true }));',
            script,
        )
        self.assertEqual(script.count("notifyTextStepsChanged(builder);"), 3)

    def test_value_rule_controls_are_wrapped_by_the_javascript_builder(self) -> None:
        template = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "mapping"
            / "_scalar_catalog.html"
        ).read_text(encoding="utf-8")

        builder = template.index("data-value-rule-builder")
        summary = template.index("<summary>", builder)
        phone_cleanup = template.index("data-use-phone-cleanup", summary)
        closing_details = template.index("</details>", phone_cleanup)

        self.assertLess(builder, summary)
        self.assertLess(summary, phone_cleanup)
        self.assertLess(phone_cleanup, closing_details)

    def test_mapping_page_owns_its_page_assets(self) -> None:
        root = REPOSITORY_ROOT
        template = (
            root / "src" / "impodo" / "web" / "templates" / "mapping" / "page.html"
        ).read_text(encoding="utf-8")
        dataset_template = (
            root
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "mapping"
            / "_dataset.html"
        ).read_text(encoding="utf-8")
        styles = (root / "src" / "impodo" / "web" / "static" / "mapping.css").read_text(
            encoding="utf-8"
        )
        script = (root / "src" / "impodo" / "web" / "static" / "mapping.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("/mapping.css", template)
        self.assertIn("/mapping-save-recovery.js", template)
        self.assertIn("/mapping-editor.js", template)
        self.assertIn("/mapping-formula-validation.js", template)
        self.assertIn("/mapping-value-rules.js", template)
        self.assertIn("/mapping-catalogs.js", template)
        self.assertIn("/mapping.js", template)
        self.assertIn(".scalar-table-scroll-top", styles)
        self.assertIn(".mapping-save-state.unsaved", styles)
        self.assertIn(".mapping-table-fields-toggle", styles)
        self.assertIn("window.impodoMappingPosition", script)
        self.assertIn("[data-mapping-form]", script)
        self.assertIn("[data-table-fields-toggle]", script)
        self.assertIn('aria-controls="mapping-table-fields-{{ dataset_index }}"', dataset_template)
        self.assertIn("data-table-fields-panel", dataset_template)
        self.assertIn("Close this table's fields", dataset_template)
        self.assertLess(
            dataset_template.index("data-table-fields-toggle"),
            dataset_template.index("data-table-fields-panel"),
        )


if __name__ == "__main__":
    unittest.main()
