"""Save, reopen, and validate an optional linked matching scope."""

from types import SimpleNamespace
from unittest import TestCase

from jinja2 import Environment, FileSystemLoader
from starlette.datastructures import FormData

from impodo.domain.mapping.contracts import IdentityNullPolicy
from impodo.web.presenters.mapping_forms import (
    _mapping_allowed_fields,
    _mapping_datasets_from_form,
)
from impodo.web.presenters.mapping_view import _mapping_dataset_views
from tests.application.workspace.mapping.test_optional_linked_scope import (
    business_keys,
    schema_models,
)
from tests.support.optional_scope import optional_scope_fixture
from tests.support.paths import REPOSITORY_ROOT


class OptionalScopeFormTests(TestCase):
    def fixture(self, *, required=False, blank_source="lot"):
        selection, _definition, _rows = optional_scope_fixture()
        schema = SimpleNamespace(
            odoo_version="20.0", models=schema_models(required=required)
        )
        governance = SimpleNamespace(business_keys=business_keys())
        form = FormData(
            [
                ("target_model_0", "x.stock"),
                ("business_key_0", "stock"),
                ("source_identity_0", "row"),
                ("identity_source_0_0", "product"),
                ("identity_source_0_1", "location"),
                ("identity_source_0_2", "lot"),
                ("identity_source_0_2", "company"),
                ("identity_origin_0_2", "target_catalog"),
                ("identity_resolver_key_0_2", "batch"),
                ("identity_blank_source_0_2", blank_source),
            ]
        )
        return selection, schema, governance, form

    def test_form_keeps_choice_when_saved_and_rendered_again(self):
        for major in (19, 20):
            with self.subTest(major=major):
                selection, schema, governance, form = self.fixture()
                schema.odoo_version = f"{major}.0"
                self.assertLessEqual(
                    set(form.keys()), _mapping_allowed_fields(form, selection, schema)
                )
                mapping = _mapping_datasets_from_form(
                    form, selection, schema, governance
                )[0]
                self.assertEqual(
                    mapping.target_scope[-1].null_policy,
                    IdentityNullPolicy.OPTIONAL_SCOPE_NULL,
                )
                self.assertEqual(
                    mapping.target_scope[-1].blank_source_column_key, "lot"
                )
                view = _mapping_dataset_views(
                    selection, schema, governance, (mapping,), active_dataset_index=0
                )[0]
                row = view["identity_rows"][-1]
                self.assertEqual(row["blank_source_column_key"], "lot")
                self.assertTrue(row["scope"])
                template = Environment(
                    loader=FileSystemLoader(
                        REPOSITORY_ROOT / "src/impodo/web/templates"
                    )
                ).get_template("mapping/_identity_blank_scope.html")
                html = template.render(
                    view=view, row=row, dataset_index=0, identity_index=2
                )
                self.assertIn("When the source is blank", html)
                self.assertIn('value="lot" selected', html)
                self.assertIn('name="identity_blank_source_0_2"', html)
                self.assertNotIn(
                    "When the source is blank",
                    template.render(
                        view=view,
                        row={**row, "metadata": SimpleNamespace(required=True)},
                        dataset_index=0,
                        identity_index=2,
                    ),
                )

    def test_required_field_cannot_accept_empty_relationship(self):
        selection, schema, governance, form = self.fixture(required=True)
        with self.assertRaisesRegex(ValueError, "optional linked matching scope"):
            _mapping_datasets_from_form(form, selection, schema, governance)

    def test_disconnected_source_column_is_rejected(self):
        selection, schema, governance, form = self.fixture(blank_source="row")
        with self.assertRaisesRegex(ValueError, "connected source column"):
            _mapping_datasets_from_form(form, selection, schema, governance)

    def test_missing_choice_keeps_strict_default(self):
        selection, schema, governance, form = self.fixture(blank_source="")
        mapping = _mapping_datasets_from_form(form, selection, schema, governance)[0]
        self.assertEqual(
            mapping.target_scope[-1].null_policy, IdentityNullPolicy.REJECT
        )
