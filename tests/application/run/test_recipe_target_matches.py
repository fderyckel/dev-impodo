"""Verify the focused Recipe target-value review mutations."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.web.recipe_target_matches import (
    TargetMatchField,
    TargetMatchReview,
    TargetValueChoice,
    TargetValueRow,
    _target_value_rows,
    apply_target_match_decisions,
)


class RecipeTargetMatchTests(unittest.TestCase):
    def test_rows_keep_verified_values_and_suggest_exact_target_choice(self):
        result = SimpleNamespace(
            distinct_values=(
                SimpleNamespace(values=("BE",), count=4),
                SimpleNamespace(values=("France",), count=2),
            ),
            uncovered_values=(("France",),),
        )
        rows = _target_value_rows(
            0,
            0,
            "relationship",
            result,
            (
                TargetValueChoice("BE", "Belgium"),
                TargetValueChoice("FR", "France"),
            ),
            {"BE": "BE"},
            exact_values={},
            submitted=None,
        )

        self.assertFalse(rows[0].needs_review)
        self.assertEqual(rows[0].current_target_label, "Belgium")
        self.assertTrue(rows[1].needs_review)
        self.assertEqual(rows[1].selected_target_value, "FR")

    def test_decisions_switch_exact_selection_to_explicit_complete_mapping(self):
        scalar = ScalarFieldMapping(
            target_field="lang",
            source_column_key="language",
            categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
        )
        dataset = DatasetMapping(
            dataset_id="contacts",
            target_model="res.partner",
            fields=(scalar,),
        )
        field = TargetMatchField(
            dataset_index=0,
            mapping_index=0,
            mapping_kind="scalar",
            dataset_name="Contacts",
            target_model="res.partner",
            target_model_label="Contact",
            target_field="lang",
            target_field_label="Language",
            source_column_label="Language",
            kind_label="Selection",
            choices=(
                TargetValueChoice("en", "English"),
                TargetValueChoice("fr_FR", "French"),
            ),
            rows=(
                TargetValueRow("en", 3, "en", "English", False, "", "en"),
                TargetValueRow(
                    "fr",
                    2,
                    "fr",
                    "fr",
                    True,
                    "match_0_scalar_0_1",
                    "fr_FR",
                ),
            ),
        )
        review = TargetMatchReview((field,), 1, None, "sha256:draft")

        updated = apply_target_match_decisions(
            SimpleNamespace(datasets=(dataset,)),
            review,
            {"match_0_scalar_0_1": "fr_FR"},
        )

        result = updated[0].fields[0]
        self.assertEqual(
            result.categorical_policy,
            CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
        )
        self.assertEqual(
            result.value_mappings,
            (
                ValueMapping("en", "en"),
                ValueMapping("fr", "fr_FR"),
            ),
        )

    def test_many2one_decision_updates_only_application_resolver(self):
        relationship = RelationshipMapping(
            target_field="country_id",
            kind="many2one",
            source_column_keys=("country",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="res.country",
                key_mappings=(ReferenceKeyMapping("country", "code"),),
                value_mappings=(ValueMapping("Belgium", "BE"),),
            ),
            categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
        )
        dataset = DatasetMapping(
            dataset_id="contacts",
            target_model="res.partner",
            relationships=(relationship,),
        )
        field = TargetMatchField(
            dataset_index=0,
            mapping_index=0,
            mapping_kind="relationship",
            dataset_name="Contacts",
            target_model="res.partner",
            target_model_label="Contact",
            target_field="country_id",
            target_field_label="Country",
            source_column_label="Country",
            kind_label="Linked record",
            choices=(
                TargetValueChoice("BE", "Belgium"),
                TargetValueChoice("FR", "France"),
            ),
            rows=(
                TargetValueRow(
                    "Belgium", 3, "BE", "Belgium", False, "", "BE"
                ),
                TargetValueRow(
                    "France",
                    2,
                    "",
                    "",
                    True,
                    "match_0_relationship_0_1",
                    "FR",
                ),
            ),
        )
        review = TargetMatchReview((field,), 1, None, "sha256:draft")

        updated = apply_target_match_decisions(
            SimpleNamespace(datasets=(dataset,)),
            review,
            {"match_0_relationship_0_1": "FR"},
        )

        self.assertEqual(
            updated[0].relationships[0].resolver.value_mappings,
            (
                ValueMapping("Belgium", "BE"),
                ValueMapping("France", "FR"),
            ),
        )
        self.assertEqual(relationship.resolver.value_mappings, (ValueMapping("Belgium", "BE"),))


if __name__ == "__main__":
    unittest.main()
