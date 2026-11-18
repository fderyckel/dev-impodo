"""Validate saved lineage and actual rule inputs without evaluating prepared references."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.mapping.contracts import (
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
)
from impodo.domain.preflight.relationship_inputs import relationship_original_inputs

_ANCHOR = "column:anchor"
_EXTRA = "column:extra"
_SCOPE = "column:scope"


def _relationship(
    preparation: ReferencePreparation, *, scoped: bool = False
) -> RelationshipMapping:
    return RelationshipMapping(
        "parent_id",
        "many2one",
        (_ANCHOR, _SCOPE) if scoped else (_ANCHOR,),
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(ReferenceKeyMapping(_ANCHOR, "ref", preparation),),
            scope_mappings=(ReferenceKeyMapping(_SCOPE, "company_id"),)
            if scoped
            else (),
        ),
    )


class RelationshipOriginalInputTests(TestCase):
    def setUp(self) -> None:
        self.preparation = ReferencePreparation(
            formula='concat("prefix/", column_2)', bindings=(("column_2", _EXTRA),)
        )
        self.relationship = _relationship(self.preparation)

    def test_formula_using_anchor_and_binding_retains_both_dependencies(self) -> None:
        relationship = _relationship(
            replace(self.preparation, formula='concat(value, "/", column_2)')
        )
        proof = relationship_original_inputs(relationship)
        self.assertEqual(
            (proof.saved_keys, proof.input_keys), ((_ANCHOR, _EXTRA), (_ANCHOR, _EXTRA))
        )

    def test_repeated_parts_and_literals_prove_each_used_source_cell_once(self) -> None:
        relationship = _relationship(
            ReferencePreparation(
                parts=(
                    ReferencePart(source_column_key=_EXTRA),
                    ReferencePart(literal="/"),
                    ReferencePart(source_column_key=_SCOPE),
                    ReferencePart(source_column_key=_EXTRA),
                )
            )
        )
        proof = relationship_original_inputs(relationship)
        self.assertEqual(proof.saved_keys, (_ANCHOR, _EXTRA, _SCOPE))
        self.assertEqual(proof.input_keys, (_EXTRA, _SCOPE))

    def test_aliases_for_one_physical_cell_do_not_duplicate_its_input(self) -> None:
        relationship = _relationship(
            ReferencePreparation(
                formula="concat(column_2, column_3)",
                bindings=(("column_2", _EXTRA), ("column_3", _EXTRA)),
            )
        )
        proof = relationship_original_inputs(relationship)
        self.assertEqual(proof.input_keys, (_EXTRA,))

    def test_actual_dependency_order_filters_the_complete_saved_envelope(self) -> None:
        proof = relationship_original_inputs(
            _relationship(self.preparation, scoped=True)
        )
        self.assertEqual(proof.saved_keys, (_ANCHOR, _SCOPE, _EXTRA))
        self.assertEqual(proof.input_keys, (_SCOPE, _EXTRA))

    def test_prepared_key_and_scope_can_share_one_real_input(self) -> None:
        relationship = _relationship(self.preparation, scoped=True)
        scope = ReferenceKeyMapping(
            _SCOPE,
            "company_id",
            ReferencePreparation(
                formula="upper(column_2)", bindings=(("column_2", _EXTRA),)
            ),
        )
        proof = relationship_original_inputs(
            replace(
                relationship,
                resolver=replace(relationship.resolver, scope_mappings=(scope,)),
            )
        )
        self.assertEqual(proof.input_keys, (_EXTRA,))

    def test_constant_key_component_with_real_scope_retains_scope_input(self) -> None:
        proof = relationship_original_inputs(
            _relationship(ReferencePreparation(formula='"FIXED"'), scoped=True)
        )
        self.assertEqual(proof.input_keys, (_SCOPE,))

    def test_literal_only_formula_or_parts_has_no_original_source_dependencies(
        self,
    ) -> None:
        for preparation in (
            ReferencePreparation(formula='"FIXED"'),
            ReferencePreparation(formula="1 + 1"),
            ReferencePreparation(parts=(ReferencePart(literal="fixed"),)),
        ):
            with self.subTest(preparation=preparation):
                proof = relationship_original_inputs(_relationship(preparation))
                self.assertEqual(proof.saved_keys, (_ANCHOR,))
                self.assertEqual(proof.input_keys, ())

    def test_invalid_syntax_or_missing_or_unused_bindings_never_get_fallback_proof(
        self,
    ) -> None:
        preparations = (
            ReferencePreparation(formula="value +"),
            ReferencePreparation(),
            ReferencePreparation(formula="concat(value, column_2)"),
            ReferencePreparation(formula="value", bindings=(("column_2", _EXTRA),)),
            ReferencePreparation(formula='__import__("os")'),
        )
        for preparation in preparations:
            with self.subTest(preparation=preparation):
                self.assertIsNone(
                    relationship_original_inputs(
                        _relationship(preparation, scoped=True)
                    )
                )

    def test_value_dependent_runtime_error_still_has_provable_static_inputs(
        self,
    ) -> None:
        proof = relationship_original_inputs(
            _relationship(ReferencePreparation(formula="value / 0"))
        )
        self.assertEqual(proof.input_keys, (_ANCHOR,))

    def test_target_first_prepared_inputs_retain_the_exact_saved_dependency_cohort(
        self,
    ) -> None:
        for scoped in (False, True):
            with self.subTest(scoped=scoped):
                relationship = _relationship(self.preparation, scoped=scoped)
                target_first = replace(
                    relationship,
                    resolver=replace(
                        relationship.resolver,
                        origin=ResolverOrigin.TARGET_THEN_DATASET,
                        dataset_id="incoming-parent",
                    ),
                )
                proof = relationship_original_inputs(target_first)
                self.assertIsNotNone(proof)
                self.assertEqual(
                    proof.saved_keys,
                    (_ANCHOR, _SCOPE, _EXTRA) if scoped else (_ANCHOR, _EXTRA),
                )
                self.assertEqual(proof.input_keys, (_SCOPE, _EXTRA) if scoped else (_EXTRA,))
                self.assertEqual(proof, relationship_original_inputs(relationship))
                self.assertEqual(target_first.resolver.key_mappings, relationship.resolver.key_mappings)
                self.assertEqual(target_first.resolver.scope_mappings, relationship.resolver.scope_mappings)

    def test_unsupported_metadata_never_authorizes_prepared_inputs(self) -> None:
        resolver = self.relationship.resolver
        relationships = (
            replace(self.relationship, kind="many2many"),
            replace(
                self.relationship,
                resolver=replace(
                    resolver,
                    origin=ResolverOrigin.DATASET,
                    dataset_id="incoming-parent",
                ),
            ),
            replace(
                self.relationship,
                resolver=replace(
                    resolver, value_mappings=(ValueMapping("West", "WEST"),)
                ),
            ),
            replace(
                self.relationship,
                resolver=replace(resolver, dataset_projection_field="display_name"),
            ),
        )
        for relationship in relationships:
            with self.subTest(relationship=relationship):
                self.assertIsNone(relationship_original_inputs(relationship))

    def test_optional_blank_choices_only_inspect_prepared_dependencies(self) -> None:
        proof = relationship_original_inputs(
            replace(self.relationship, blank_source_values=("Leave blank",))
        )
        self.assertEqual(proof.input_keys, (_EXTRA,))
