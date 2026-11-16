"""Explain computed reference inputs in review exports without leaking literals."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.mapping.contracts import (
    MAPPING_CONTRACT_VERSION,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.mapping.reference_preparation import ReferencePart, ReferencePreparation
from impodo.domain.preflight.mapping_summary import workbook_mapping_rules
from tests.integration.artifacts import test_mapping_review_workbook as fixtures


class PreparedReferenceSummaryTests(TestCase):
    def rules(self, preparation, *, identity=False, restricted=False):
        revision, _, selection, _ = fixtures.MappingReviewWorkbookTests()._evidence()
        resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(ReferenceKeyMapping("name", "ref", preparation),),
        )
        changes = (
            {"target_scope": (IdentityComponentMapping(("name",), ("partner_id",), resolver=resolver),)}
            if identity else
            {"relationships": (RelationshipMapping("partner_id", "many2one", ("name",), resolver),)}
        )
        mapping = replace(revision.definition.datasets[0], **changes)
        definition = replace(revision.definition, datasets=(mapping,), contract_version=MAPPING_CONTRACT_VERSION)
        return workbook_mapping_rules(definition, selection, restricted=restricted)[("Orders", "partner_id")]

    def test_formula_inputs_and_bindings_are_retained_for_both_reference_owners(self):
        preparation = ReferencePreparation(
            formula='concat(value, "/private-prefix/", column_2)',
            bindings=(("column_2", "external_id"),),
        )
        for identity in (False, True):
            with self.subTest(identity=identity):
                rule = self.rules(preparation, identity=identity)
                self.assertEqual(rule.source_labels, ("Order Reference", "External ID"))
                self.assertIn("column_2 = External ID", "\n".join(rule.steps))
                self.assertIn("/private-prefix/", "\n".join(rule.steps))
                restricted = self.rules(preparation, identity=identity, restricted=True)
                self.assertNotIn("private-prefix", "\n".join(restricted.steps))
                self.assertIn("hidden for restricted data", "\n".join(restricted.steps))

    def test_combination_preserves_component_order_and_withholds_literals(self):
        preparation = ReferencePreparation(parts=(
            ReferencePart(source_column_key="name"),
            ReferencePart(literal="/private-prefix/"),
            ReferencePart(source_column_key="external_id"),
        ))
        text = "\n".join(self.rules(preparation).steps)
        self.assertIn('Combine in order: Order Reference, "/private-prefix/", External ID', text)
        self.assertNotIn("private-prefix", "\n".join(self.rules(preparation, restricted=True).steps))
