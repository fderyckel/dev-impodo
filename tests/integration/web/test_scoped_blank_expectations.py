"""Opening checked matching expectations preserves scoped leave-blank decisions."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from impodo.domain.mapping.contracts import ReferenceKeyMapping, ValueMapping
from impodo.domain.workspace.supporting_lookups import portable_supporting_value
from impodo.web.matching_expectations import matching_expectations_view
from tests.support.business_intent import ACTOR, metadata_fixture


class ScopedBlankExpectationsTests(unittest.TestCase):
    def test_checked_page_projection_accepts_scoped_blanks_for_standard_and_custom_models(self):
        for version in ("19.0", "20.0"):
            for parent, field_name, related in (
                ("res.partner", "property_payment_term_id", "account.payment.term"),
                ("x.document", "x_lookup_id", "x.lookup"),
            ):
                with self.subTest(version=version, parent=parent):
                    fixture = metadata_fixture(odoo_version=version)
                    mapping = fixture.mapping.definition.datasets[0]
                    relation = replace(
                        mapping.relationships[0], target_field=field_name,
                        blank_source_values=("OMIT",), resolver=replace(
                            mapping.relationships[0].resolver, model=related,
                            key_mappings=(ReferenceKeyMapping("category", "name"),),
                            scope_mappings=(ReferenceKeyMapping("category", "company_id"),),
                            value_mappings=(ValueMapping("source-choice", portable_supporting_value(("Matched record", "Company A"))),),
                        ),
                    )
                    definition = replace(fixture.mapping.definition, datasets=(replace(
                        mapping, target_model=parent, relationships=(relation,),
                    ),))
                    schema = replace(fixture.schema, models=(replace(
                        fixture.schema.models[0], name=parent, fields=tuple(
                            replace(field, name=field_name, relation=related) if field.name == "country_id" else field
                            for field in fixture.schema.models[0].fields
                        ),
                    ),))
                    revision = replace(fixture.mapping, definition=definition)
                    validation = replace(fixture.validation, mapping_content_hash=definition.content_hash)
                    context = SimpleNamespace(
                        business_intents=SimpleNamespace(get=lambda *args, **kwargs: None),
                        workspace_access=SimpleNamespace(resolve=lambda *args, **kwargs: fixture.access),
                    )
                    before = definition.to_json()
                    view = matching_expectations_view(
                        context, workspace=fixture.workspace, actor=ACTOR,
                        selection=fixture.selection, schema=schema,
                        governance=fixture.governance, revision=revision,
                        validation=validation, dirty=False,
                    )
                    self.assertIsNotNone(view["packet"], view["message"])
                    self.assertTrue(view["rows"])
                    self.assertEqual(view["rows"][0]["scope"].model, parent)
                    self.assertEqual(definition.to_json(), before)
