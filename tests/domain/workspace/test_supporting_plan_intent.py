"""Only actual relationship use and required fields constrain file migrations."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping
from impodo.domain.workspace.supporting_closure import (
    close_supporting_plan,
    portable_supporting_choices,
    recipe_supporting_choices,
    supporting_mapping_conflicts,
)
from impodo.domain.workspace.supporting_errors import SupportingPlanIncompleteError
from impodo.domain.workspace.supporting_plan import SUPPORTING_PLAN_VERSION, SupportingPlanGap
from impodo.domain.workspace.supporting_review import SupportingChoice
from tests.domain.workspace.test_supporting_closure import relationship
from tests.domain.workspace.test_supporting_plan import dataset, model, relation


class SupportingPlanIntentTests(unittest.TestCase):
    def test_same_rules_apply_to_standard_and_custom_models_and_field_names(self):
        for version in ("19.0+e", "20.0+e"):
            for parent, collection, child, inverse in (
                ("res.partner", "bank_ids", "res.partner.bank", "partner_id"),
                ("mrp.bom", "bom_line_ids", "mrp.bom.line", "bom_id"),
                ("x.migration_header", "x_detail_ids", "x.migration_detail", "x_header_id"),
            ):
                with self.subTest(version=version, model=parent):
                    field = replace(relation(collection, child, required=False, kind="one2many"), relation_field=inverse)
                    schema = SimpleNamespace(odoo_version=version, content_hash="schema", models=(
                        model(parent, field), model(child, relation(inverse, parent)),
                    ))
                    for choices in ((), (SupportingChoice(parent, collection, "SKIP_OPTIONAL"),)):
                        result = close_supporting_plan(
                            schema=schema, datasets=(dataset("headers"),), mappings=(DatasetMapping("headers", parent),),
                            choices=choices, source_hash="source", preparation_hash=None,
                        )
                        self.assertTrue(result.proposal.complete, result.proposal.issues)
                        self.assertEqual(result.proposal.edges, ())
                    active = close_supporting_plan(
                        schema=schema, datasets=(dataset("headers"), dataset("details")),
                        mappings=(DatasetMapping("headers", parent), DatasetMapping("details", child,
                            relationships=(relationship(inverse, parent, incoming="headers"),))),
                        choices=(), source_hash="source", preparation_hash=None,
                    )
                    self.assertTrue(active.proposal.complete, active.proposal.issues)
                    self.assertEqual({x.field_name for x in active.proposal.edges}, {collection, inverse})
                    required_schema = SimpleNamespace(odoo_version=version, content_hash="schema", models=(
                        model(parent, replace(field, required=True)),
                    ))
                    missing = close_supporting_plan(
                        schema=required_schema, datasets=(dataset("headers"),), mappings=(DatasetMapping("headers", parent),),
                        choices=(), source_hash="source", preparation_hash=None,
                    )
                    self.assertEqual(missing.proposal.issues[0].reason, SupportingPlanGap.INCOMING_UNAVAILABLE)

    def close(self, version, fields, *, choices=(), mappings=None, extras=(), descriptors=()):
        schema = SimpleNamespace(
            odoo_version=version, content_hash="schema",
            models=(model("res.partner", *fields), *extras),
        )
        mappings = mappings or (DatasetMapping("contacts", "res.partner"),)
        result = close_supporting_plan(
            schema=schema, datasets=(dataset("contacts"), *descriptors),
            mappings=mappings, choices=choices, source_hash="source", preparation_hash=None,
        )
        return result, schema, mappings

    def test_unused_and_explicitly_skipped_optional_collections_do_not_block(self):
        fields = tuple(replace(
            relation(name, related, required=False, kind="one2many"),
            relation_field="partner_id",
        ) for name, related in (
            ("activity_ids", "mail.activity"), ("invoice_ids", "account.move"),
            ("user_ids", "res.users"),
        ))
        for version in ("19.0+e", "20.0+e"):
            for choices in ((), tuple(SupportingChoice("res.partner", f.name, "SKIP_OPTIONAL") for f in fields)):
                with self.subTest(version=version, choices=bool(choices)):
                    result, schema, mappings = self.close(version, fields, choices=choices)
                    self.assertTrue(result.proposal.complete, result.proposal.issues)
                    self.assertEqual(result.proposal.edges, ())
                    self.assertEqual(supporting_mapping_conflicts(choices, mappings, schema), ())

    def test_reviewed_treatment_precedes_candidate_expansion(self):
        descriptors = (dataset("a", "x.category"), dataset("b", "x.category"))
        for version in ("19.0+e", "20.0+e"):
            for flags, action, role in (
                ({}, "REUSE", "REUSE_EXISTING"),
                ({"create_default_present": True}, "CHECKED_DEFAULT", "CHECKED_DEFAULT"),
                ({"computed": True}, "ODOO_MANAGED", "ODOO_MANAGED"),
            ):
                with self.subTest(version=version, action=action):
                    field = relation("category_id", "x.category", required=False, **flags)
                    choice = SupportingChoice("res.partner", "category_id", action,
                                              key_fields=("name",) if action == "REUSE" else ())
                    result, _schema, _mappings = self.close(
                        version, (field,), choices=(choice,), descriptors=descriptors,
                    )
                    self.assertTrue(result.proposal.complete, result.proposal.issues)
                    self.assertEqual(result.proposal.edges[0].role.value, role)
                    self.assertEqual(result.proposal.edges[0].candidates, ())

    def test_actual_target_only_mapping_does_not_expand_unused_incoming_candidates(self):
        for version in ("19.0+e", "20.0+e"):
            with self.subTest(version=version):
                mappings = (DatasetMapping("contacts", "res.partner", relationships=(relationship("category_id", "x.category"),)),)
                result, _, _ = self.close(
                    version, (relation("category_id", "x.category", required=False),), mappings=mappings,
                    descriptors=(dataset("a", "x.category"), dataset("b", "x.category")),
                )
                self.assertTrue(result.proposal.complete, result.proposal.issues)
                self.assertEqual(result.proposal.edges[0].role.value, "REUSE_EXISTING")
                self.assertEqual(result.proposal.edges[0].candidates, ())

    def test_company_dependent_and_nonexportable_fields_keep_their_existing_owner(self):
        for version in ("19.0+e", "20.0+e"):
            for flags in ({"company_dependent": True}, {"exportable": False}):
                with self.subTest(version=version, flags=flags):
                    mappings = (DatasetMapping("contacts", "res.partner", relationships=(relationship("property_payment_term_id", "account.payment.term"),)),)
                    result, _, _ = self.close(
                        version, (relation("property_payment_term_id", "account.payment.term", required=False, **flags),),
                        mappings=mappings,
                    )
                    self.assertTrue(result.proposal.complete, result.proposal.issues)
                    self.assertEqual(result.proposal.edges, ())

    def test_actual_child_ownership_survives_and_cannot_be_skipped(self):
        inverse = replace(relation("bank_ids", "res.partner.bank", required=False, kind="one2many"), relation_field="partner_id")
        mappings = (
            DatasetMapping("contacts", "res.partner"),
            DatasetMapping("banks", "res.partner.bank", relationships=(relationship("partner_id", "res.partner", incoming="contacts"),)),
        )
        for version in ("19.0+e", "20.0+e"):
            for identity_provider in (False, True):
                with self.subTest(version=version, identity_provider=identity_provider):
                    actual = mappings
                    if identity_provider:
                        actual = (mappings[0], replace(mappings[1], relationships=(), target_identity=(
                            IdentityComponentMapping(("source",), ("partner_id",), resolver=mappings[1].relationships[0].resolver),
                        )))
                    result, schema, _ = self.close(
                        version, (inverse,), mappings=actual,
                        extras=(model("res.partner.bank", relation("partner_id", "res.partner")),),
                        descriptors=(dataset("banks"),),
                    )
                    self.assertTrue(result.proposal.complete, result.proposal.issues)
                    edge = next(x for x in result.proposal.edges if x.field_name == "bank_ids")
                    self.assertEqual(edge.candidates[0].dataset_id, "banks")
                    skipped = (SupportingChoice("res.partner", "bank_ids", "SKIP_OPTIONAL"),)
                    self.assertTrue(supporting_mapping_conflicts(skipped, actual, schema))

    def test_required_and_incoming_metadata_gaps_remain_named_blockers(self):
        required = replace(relation("bank_ids", "res.partner.bank", kind="one2many"), relation_field="partner_id")
        for version in ("19.0+e", "20.0+e"):
            with self.subTest(version=version):
                result, schema, mappings = self.close(version, (required,))
                self.assertFalse(result.proposal.complete)
                self.assertEqual(result.proposal.issues[0].reason, SupportingPlanGap.INCOMING_UNAVAILABLE)
                error = SupportingPlanIncompleteError(result.proposal, schema, mappings, ())
                self.assertIn("res.partner.bank_ids", str(error))
                self.assertEqual(error.odoo_major, int(version[:2]))
                self.assertTrue(error.issues[0].required)
                self.assertFalse(error.issues[0].used_by_mapping)
                chosen = (SupportingChoice("res.partner", "bank_ids", "INCOMING", "missing"),)
                result, _, _ = self.close(version, (required,), choices=chosen)
                self.assertFalse(result.proposal.complete)

    def test_incoming_child_must_belong_to_each_exact_parent_table(self):
        inverse = replace(relation("bank_ids", "res.partner.bank", required=False, kind="one2many"), relation_field="partner_id")
        for version in ("19.0+e", "20.0+e"):
            with self.subTest(version=version):
                schema = SimpleNamespace(odoo_version=version, models=(
                    model("res.partner", inverse), model("res.partner.bank", relation("partner_id", "res.partner")),
                ))
                mappings = (
                    DatasetMapping("contacts_a", "res.partner"), DatasetMapping("contacts_b", "res.partner"),
                    DatasetMapping("banks", "res.partner.bank", relationships=(relationship("partner_id", "res.partner", incoming="contacts_a"),)),
                )
                choices = (SupportingChoice("res.partner", "bank_ids", "INCOMING", "banks"),)
                self.assertTrue(supporting_mapping_conflicts(choices, mappings, schema))

    def test_recipe_rebound_skip_uses_the_same_closure_and_keeps_evidence(self):
        inverse = replace(relation("activity_ids", "mail.activity", required=False, kind="one2many"), relation_field="res_id")
        choices = (SupportingChoice("res.partner", "activity_ids", "SKIP_OPTIONAL"),)
        portable = portable_supporting_choices(choices, {})
        rebound = recipe_supporting_choices({"supporting_choices": portable}, {})
        for version in ("19.0+e", "20.0+e"):
            with self.subTest(version=version):
                result, _, _ = self.close(version, (inverse,), choices=rebound)
                self.assertTrue(result.proposal.complete)
                self.assertEqual(result.choices, choices)
                self.assertEqual(result.proposal.contract_version, SUPPORTING_PLAN_VERSION)
                self.assertEqual(result.proposal.source_selection_hash, "source")


if __name__ == "__main__":
    unittest.main()
