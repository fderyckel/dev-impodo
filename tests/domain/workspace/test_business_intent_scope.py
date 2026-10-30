"""Business expectations narrow the same qualified modes and fields as execution."""

from dataclasses import replace
import unittest

from impodo.domain.mapping.contracts import MappingTargetMode
from impodo.domain.odoo.compatibility import OdooOperation
from impodo.domain.workspace.business_intent import BusinessIntent, BusinessIntentError, BusinessModelIntent, BusinessRelationshipRule, ExpectedCount
from impodo.domain.workspace.business_intent_scope import derive_business_intent_scope
from impodo.domain.workspace.workbench import SourceMode
from tests.support.business_intent import metadata_fixture


def scope(fixture, *, operation=OdooOperation.WRITE):
    return derive_business_intent_scope(fixture.mapping.definition, fixture.selection, fixture.schema, source_mode=fixture.workspace.source_mode, operation=operation)


class BusinessIntentScopeTests(unittest.TestCase):
    def test_update_only_expectation_narrows_upsert_and_preserves_explicit_zero(self):
        current = scope(metadata_fixture())
        current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "update", update_fields=("email",), creates=ExpectedCount(maximum=0), updates=ExpectedCount(1, 200)),)))
        self.assertEqual(current.models[0].create_fields, ("email", "initial_note", "name", "ref"))
        self.assertEqual(current.models[0].update_fields, ("email", "name"))

    def test_default_validate_only_and_create_only_fields_cannot_be_enabled_for_updates(self):
        current = scope(metadata_fixture())
        for field in ("default_field", "check_only", "initial_note", "ref", "unknown", "id"):
            with self.subTest(field=field), self.assertRaisesRegex(BusinessIntentError, "field limits"):
                current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "update", update_fields=(field,)),)))

    def test_relationship_operations_preserve_authored_semantics(self):
        current = scope(metadata_fixture())
        current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "update", update_relationships=(BusinessRelationshipRule("category_ids", ("add",)),)),)))
        for field, operation in (("category_ids", "replace"), ("country_id", "add"), ("unknown", "remove")):
            with self.subTest(field=field), self.assertRaisesRegex(BusinessIntentError, "relationship operations"):
                current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "update", update_relationships=(BusinessRelationshipRule(field, (operation,)),)),)))

    def test_create_and_reference_modes_cannot_be_promoted_by_count_limits(self):
        for mode in (MappingTargetMode.CREATE, MappingTargetMode.REFERENCE):
            current = scope(metadata_fixture(mode=mode))
            with self.assertRaisesRegex(BusinessIntentError, "write mode"):
                current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "upsert", updates=ExpectedCount(maximum=0)),)))
            current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "none"),)))
        with self.assertRaisesRegex(BusinessIntentError, "outside"):
            scope(metadata_fixture()).require_narrowing(BusinessIntent((BusinessModelIntent("product.template", "none"),)))

    def test_odoo_version_and_production_gates_remain_outer_constraints(self):
        scope(metadata_fixture(odoo_version="20.0")).require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "upsert"),)))
        for version in ("18.0", "20.0rc1", "unknown"):
            with self.subTest(version=version), self.assertRaisesRegex(BusinessIntentError, "write mode"):
                scope(metadata_fixture(odoo_version=version)).require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "upsert"),)))
        with self.assertRaisesRegex(BusinessIntentError, "write mode"):
            scope(metadata_fixture(odoo_version="20.0"), operation=OdooOperation.PRODUCTION).require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "upsert"),)))

    def test_pinned_odoo_updates_reuse_tier_one_approval_and_never_enable_create(self):
        fixture = metadata_fixture(source_mode=SourceMode.ODOO, mode=MappingTargetMode.ODOO_PINNED_UPDATE)
        current = scope(fixture)
        current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "update", update_fields=("email",)),)))
        with self.assertRaisesRegex(BusinessIntentError, "write mode"):
            current.require_narrowing(BusinessIntent((BusinessModelIntent("res.partner", "upsert"),)))
        fields = tuple(replace(item, stored=None) if item.name == "email" else item for item in fixture.schema.models[0].fields)
        fixture.schema = replace(fixture.schema, models=(replace(fixture.schema.models[0], fields=fields),))
        with self.assertRaisesRegex(BusinessIntentError, "writable scope"):
            scope(fixture)
