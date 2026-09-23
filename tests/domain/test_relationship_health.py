from __future__ import annotations

import unittest

from impodo.domain.mapping.contracts import ResolverOrigin
from impodo.domain.relationship_health import classify_relationship_health


class RelationshipHealthTests(unittest.TestCase):
    def test_target_first_counts_rows_and_keeps_resolution_causes_distinct(self) -> None:
        result = classify_relationship_health(
            owner_dataset_id="bom-lines",
            target_field="bom_id",
            relationship_kind="many2one",
            resolver_origin=ResolverOrigin.TARGET_THEN_DATASET,
            related_model="mrp.bom",
            dependency_dataset_id="boms",
            required=True,
            source_row_count=9,
            source_key_counts={
                ("TARGET",): 2,
                ("INCOMING",): 1,
                ("MISSING",): 1,
                ("DUPLICATE",): 1,
                ("wrong case",): 1,
            },
            blank_row_count=1,
            incomplete_row_count=2,
            target_keys=(
                ("TARGET",),
                ("DUPLICATE",),
                ("DUPLICATE",),
                ("Wrong Case",),
            ),
            incoming_key_counts={("INCOMING",): 1},
        )

        self.assertEqual(result.populated_choice_count, 6)
        self.assertEqual(result.target_count, 2)
        self.assertEqual(result.incoming_count, 1)
        self.assertEqual(result.missing_count, 1)
        self.assertEqual(result.ambiguous_count, 1)
        self.assertEqual(result.case_mismatch_count, 1)
        self.assertEqual(result.blocked_count, 6)
        self.assertFalse(result.ready)

    def test_case_only_target_blocks_incoming_fallback(self) -> None:
        result = classify_relationship_health(
            owner_dataset_id="boms",
            target_field="product_id",
            relationship_kind="many2one",
            resolver_origin=ResolverOrigin.TARGET_THEN_DATASET,
            related_model="product.product",
            dependency_dataset_id="products",
            required=True,
            source_row_count=1,
            source_key_counts={("p001",): 1},
            blank_row_count=0,
            incomplete_row_count=0,
            target_keys=(("P001",),),
            incoming_key_counts={("p001",): 1},
        )

        self.assertEqual(result.case_mismatch_count, 1)
        self.assertEqual(result.incoming_count, 0)

    def test_optional_blank_relationship_can_be_ready(self) -> None:
        result = classify_relationship_health(
            owner_dataset_id="products",
            target_field="category_id",
            relationship_kind="many2one",
            resolver_origin=ResolverOrigin.TARGET_CATALOG,
            related_model="product.category",
            dependency_dataset_id=None,
            required=False,
            source_row_count=2,
            source_key_counts={("ALL",): 1},
            blank_row_count=1,
            incomplete_row_count=0,
            target_keys=(("ALL",),),
            incoming_key_counts={},
        )

        self.assertTrue(result.ready)
        self.assertEqual(result.blocked_count, 0)


if __name__ == "__main__":
    unittest.main()
