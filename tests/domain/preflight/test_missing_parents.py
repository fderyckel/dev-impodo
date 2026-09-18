"""Missing parents must come from structured, exact comparison evidence."""

from __future__ import annotations

import unittest

from impodo.domain.mapping.source_conditions import parse_source_text_list
from impodo.domain.preflight.missing_parents import missing_parent_groups


class MissingParentGroupsTests(unittest.TestCase):
    def test_groups_exact_bom_values_and_counts_each_operation_once(self) -> None:
        values = (
            "CK0291ABK332 -  Idru",
            "CK0291ABK378  LT004",
            "CK0291ABK466  LT002",
            "CK0291ABK467  LT002",
        )
        resolutions = []
        for value in values:
            reference = {
                "origin": "target", "model": "mrp.bom", "key": [value],
                "scope": [], "target_fields": ["code"],
            }
            for field in ("target_identity:bom_id", "bom_id"):
                resolutions.append({
                    "dataset": "plw_bom_operation", "field": field,
                    "reference": reference, "status": "NOT_FOUND",
                    "match_count": 0, "affected_count": 9,
                })
        resolutions.append({
            "dataset": "plw_bom_operation", "field": "bom_id",
            "reference": {"origin": "target", "model": "mrp.bom",
                          "key": ["AMBIGUOUS"], "scope": [],
                          "target_fields": ["code"]},
            "status": "AMBIGUOUS", "match_count": 2, "affected_count": 3,
        })

        groups = missing_parent_groups(resolutions)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].affected_count, 36)
        self.assertEqual(groups[0].copy_text, ", ".join(values))
        self.assertTrue(groups[0].stage3_list_supported)
        self.assertEqual(parse_source_text_list(groups[0].copy_text), values)
        self.assertEqual(tuple(item.affected_count for item in groups[0].values),
                         (9, 9, 9, 9))

    def test_separates_company_contexts_and_ignores_failed_reads(self) -> None:
        base = {
            "dataset": "operations", "field": "bom_id", "status": "NOT_FOUND",
            "match_count": 0, "affected_count": 2,
        }
        references = (
            {"origin": "target", "model": "mrp.bom", "key": ["B1"],
             "scope": ["Wiltz"], "target_fields": ["code"],
             "target_scope_fields": ["company_id"]},
            {"origin": "target", "model": "mrp.bom", "key": ["B1"],
             "scope": ["France"], "target_fields": ["code"],
             "target_scope_fields": ["company_id"]},
        )
        groups = missing_parent_groups([
            {**base, "reference": reference} for reference in references
        ] + [{**base, "status": "READ_FAILED", "reference": references[0]}])
        self.assertEqual(len(groups), 2)
        self.assertEqual({group.scope_values for group in groups},
                         {("Wiltz",), ("France",)})

    def test_hybrid_reference_uses_exact_incoming_value_for_copy(self) -> None:
        groups = missing_parent_groups([{
            "dataset": "operations", "field": "bom_id", "status": "NOT_FOUND",
            "match_count": 0, "affected_count": 1,
            "reference": {
                "origin": "target_then_incoming", "model": "mrp.bom",
                "key": ["canonical-bom"],
                "incoming_key": ["CK0291ABK332 -  Idru"],
                "scope": [], "target_fields": ["code"],
            },
        }])
        self.assertEqual(groups[0].copy_text, "CK0291ABK332 -  Idru")
