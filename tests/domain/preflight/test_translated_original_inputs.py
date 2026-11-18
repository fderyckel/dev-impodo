"""Prove scoped reviewed translations consume one physical source choice."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    ValueMapping,
    relationship_input_columns,
)
from impodo.domain.mapping.reference_preparation import ReferencePreparation
from impodo.domain.preflight.relationship_inputs import relationship_original_inputs

_CHOICE = "column:choice"
_OTHER = "column:other"
_PAYLOAD = '["WEST","Company A"]'


def _relationship() -> RelationshipMapping:
    return RelationshipMapping(
        "parent_id",
        "many2one",
        (_CHOICE,),
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(ReferenceKeyMapping(_CHOICE, "ref"),),
            scope_mappings=(ReferenceKeyMapping(_CHOICE, "company_id"),),
            value_mappings=(ValueMapping("West", _PAYLOAD),),
        ),
    )


class TranslatedOriginalInputTests(TestCase):
    def test_repeated_key_and_scope_slots_prove_one_saved_and_actual_input(
        self,
    ) -> None:
        relationship = _relationship()
        self.assertEqual(relationship_input_columns(relationship), (_CHOICE, _CHOICE))
        proof = relationship_original_inputs(relationship)
        self.assertEqual((proof.saved_keys, proof.input_keys), ((_CHOICE,), (_CHOICE,)))
        self.assertEqual(relationship.resolver.value_mappings[0].target_value, _PAYLOAD)

    def test_multiple_scope_components_still_use_only_the_single_choice(self) -> None:
        relationship = _relationship()
        relationship = replace(
            relationship,
            resolver=replace(
                relationship.resolver,
                scope_mappings=(
                    *relationship.resolver.scope_mappings,
                    ReferenceKeyMapping(_CHOICE, "country_id"),
                ),
                value_mappings=(
                    ValueMapping("West", '["WEST","Company A","Country"]'),
                ),
            ),
        )
        proof = relationship_original_inputs(relationship)
        self.assertEqual(proof.input_keys, (_CHOICE,))

    def test_every_selected_tuple_must_have_valid_complete_text_components(
        self,
    ) -> None:
        invalid = (
            "not JSON",
            '"just text"',
            "{}",
            '["WEST"]',
            '["WEST","Company","Extra"]',
            '["WEST",null]',
            '["WEST",false]',
            '["WEST",0]',
            '["WEST",""]',
            '["WEST","  "]',
        )
        relationship = _relationship()
        for payload in invalid:
            with self.subTest(payload=payload):
                changed = replace(
                    relationship,
                    resolver=replace(
                        relationship.resolver,
                        value_mappings=(
                            ValueMapping("West", _PAYLOAD),
                            ValueMapping("ZZZ malformed", payload),
                        ),
                    ),
                )
                self.assertIsNone(relationship_original_inputs(changed))

    def test_zero_scope_text_is_valid_metadata_without_reinterpreting_it(self) -> None:
        relationship = _relationship()
        payload = '["WEST","0"]'
        relationship = replace(
            relationship,
            resolver=replace(
                relationship.resolver, value_mappings=(ValueMapping("West", payload),)
            ),
        )
        self.assertEqual(
            relationship_original_inputs(relationship).input_keys, (_CHOICE,)
        )
        self.assertEqual(relationship.resolver.value_mappings[0].target_value, payload)

    def test_nonmatching_extra_duplicate_or_malformed_component_sources_are_unproven(
        self,
    ) -> None:
        relationship = _relationship()
        resolver = relationship.resolver
        invalid = (
            replace(relationship, source_column_keys=(_CHOICE, _OTHER)),
            replace(
                relationship,
                resolver=replace(
                    resolver,
                    scope_mappings=(ReferenceKeyMapping(_OTHER, "company_id"),),
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver, key_mappings=(ReferenceKeyMapping(_OTHER, "ref"),)
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver,
                    key_mappings=(
                        *resolver.key_mappings,
                        ReferenceKeyMapping(_CHOICE, "code"),
                    ),
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver, scope_mappings=(ReferenceKeyMapping(_CHOICE, "ref"),)
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver, scope_mappings=(ReferenceKeyMapping(" ", "company_id"),)
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver, scope_mappings=(ReferenceKeyMapping(_CHOICE, " "),)
                ),
            ),
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                self.assertIsNone(relationship_original_inputs(changed))

    def test_incoming_hybrid_list_projection_and_preparation_do_not_qualify(
        self,
    ) -> None:
        relationship = _relationship()
        resolver = relationship.resolver
        invalid = (
            replace(relationship, kind="many2many"),
            replace(
                relationship,
                resolver=replace(
                    resolver, origin=ResolverOrigin.DATASET, dataset_id="parent-dataset"
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver,
                    origin=ResolverOrigin.TARGET_THEN_DATASET,
                    dataset_id="parent-dataset",
                ),
            ),
            replace(
                relationship,
                resolver=replace(resolver, dataset_projection_field="display_name"),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver,
                    key_mappings=(
                        ReferenceKeyMapping(
                            _CHOICE, "ref", ReferencePreparation(formula="value")
                        ),
                    ),
                ),
            ),
            replace(
                relationship,
                resolver=replace(
                    resolver,
                    scope_mappings=(
                        ReferenceKeyMapping(
                            _CHOICE, "company_id", ReferencePreparation()
                        ),
                    ),
                ),
            ),
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                self.assertIsNone(relationship_original_inputs(changed))

    def test_repeated_scope_without_reviewed_translations_has_no_proof(self) -> None:
        relationship = _relationship()
        changed = replace(
            relationship, resolver=replace(relationship.resolver, value_mappings=())
        )
        self.assertIsNone(relationship_original_inputs(changed))

    def test_deliberate_blank_choices_and_blank_create_fallback_add_no_source_inputs(
        self,
    ) -> None:
        relationship = _relationship()
        for changed in (
            replace(relationship, blank_source_values=("OMIT",)),
            replace(relationship, blank_create_values=("FIXED", "Company A")),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(
                    relationship_original_inputs(changed).input_keys, (_CHOICE,)
                )

    def test_fixed_existing_reference_has_a_known_empty_source_cohort(self) -> None:
        relationship = RelationshipMapping(
            "parent_id",
            "many2one",
            (),
            RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.partner"),
            value_source=RelationshipValueSource.CONSTANT_EXISTING,
            constant_reference=ConstantBusinessReference(
                (ConstantReferenceComponent("ref", "WEST"),),
                (ConstantReferenceComponent("company_id", "Company A"),),
            ),
        )
        for kind in ("many2one", "many2many"):
            with self.subTest(kind=kind):
                constant = replace(relationship, kind=kind)
                proof = relationship_original_inputs(constant)
                self.assertIsNotNone(proof)
                self.assertEqual(proof.saved_keys, ())
                self.assertEqual(proof.input_keys, ())
                self.assertEqual(constant.source_column_keys, ())
                self.assertEqual(constant.constant_reference, relationship.constant_reference)
