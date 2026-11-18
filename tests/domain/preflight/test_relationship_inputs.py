"""Prove complete relationship input cohorts without resolving source values."""

from __future__ import annotations

import unittest
from dataclasses import replace

from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.mapping.reference_preparation import ReferencePreparation
from impodo.domain.preflight.relationship_inputs import (
    RelationshipOriginalInputs,
    relationship_original_inputs,
)

_KEYS = ("column:region", "column:code")


def _composite() -> RelationshipMapping:
    return RelationshipMapping(
        "parent_id", "many2one", _KEYS,
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG, model="res.partner",
            key_mappings=tuple(
                ReferenceKeyMapping(key, target)
                for key, target in zip(_KEYS, ("region", "code"), strict=True)
            ),
        ),
    )


class RelationshipInputProofTests(unittest.TestCase):
    def test_unscoped_composite_key_requires_one_exact_ordered_cohort(self) -> None:
        relationship = _composite()
        for origin in (ResolverOrigin.TARGET_CATALOG, ResolverOrigin.TARGET_THEN_DATASET):
            with self.subTest(origin=origin):
                actual = replace(
                    relationship, resolver=replace(relationship.resolver, origin=origin)
                )
                self.assertEqual(
                    relationship_original_inputs(actual), RelationshipOriginalInputs(_KEYS, _KEYS)
                )

    def test_partial_reordered_duplicate_or_translated_composite_keys_are_unproven(self) -> None:
        relationship = _composite()
        resolver = relationship.resolver
        invalid = (
            replace(relationship, source_column_keys=tuple(reversed(_KEYS))),
            replace(relationship, source_column_keys=(_KEYS[0],) * 2),
            replace(relationship, resolver=replace(resolver, key_mappings=resolver.key_mappings[:1])),
            replace(relationship, resolver=replace(resolver, key_mappings=tuple(reversed(resolver.key_mappings)))),
            replace(relationship, resolver=replace(resolver, key_mappings=(
                resolver.key_mappings[0], ReferenceKeyMapping(_KEYS[1], "region"),
            ))),
            replace(relationship, resolver=replace(resolver, value_mappings=(ValueMapping("A", "B"),))),
            replace(relationship, kind="many2many"),
            replace(relationship, resolver=replace(resolver, dataset_projection_field="name")),
        )
        for actual in invalid:
            with self.subTest(actual=actual):
                self.assertIsNone(relationship_original_inputs(actual))

    def test_target_first_preparation_has_the_same_file_dependencies_as_target_only(self) -> None:
        relationship = _composite()
        prepared = replace(
            relationship, resolver=replace(
                relationship.resolver,
                key_mappings=(ReferenceKeyMapping(
                    _KEYS[0], "region",
                    ReferencePreparation(formula="concat(value, column_2)", bindings=(("column_2", _KEYS[1]),)),
                ), ReferenceKeyMapping(_KEYS[1], "code")),
            ),
        )
        proof = relationship_original_inputs(prepared)
        self.assertEqual(proof, RelationshipOriginalInputs(_KEYS, _KEYS))
        self.assertEqual(
            relationship_original_inputs(replace(
                prepared, resolver=replace(prepared.resolver, origin=ResolverOrigin.TARGET_THEN_DATASET)
            )),
            proof,
        )

    def test_constant_existing_relationship_proves_no_source_cell(self) -> None:
        for kind in ("many2one", "many2many"):
            with self.subTest(kind=kind):
                relationship = RelationshipMapping(
                    "parent_id", kind, (),
                    RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.partner"),
                    value_source=RelationshipValueSource.CONSTANT_EXISTING,
                    constant_reference=ConstantBusinessReference(
                        (ConstantReferenceComponent("ref", "WEST"),)
                    ),
                )
                self.assertEqual(
                    relationship_original_inputs(relationship), RelationshipOriginalInputs((), ())
                )


if __name__ == "__main__":
    unittest.main()
