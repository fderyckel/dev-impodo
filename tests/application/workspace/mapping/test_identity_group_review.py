"""Keep duplicate membership aligned with complete normalized identity semantics."""

import unittest
from dataclasses import asdict

from impodo.application.workspace.mapping.identity_group_review import (
    identity_duplicate_choices,
)
from impodo.domain.mapping.contracts import MappingTargetMode
from impodo.domain.matching_order import (
    MatchingIdentityChoice,
    MatchingIdentityChoiceFilter,
    classify_indexed_matching_identity,
    index_matching_identity_keys,
)
from impodo.domain.workspace.errors import WorkspaceError


class IdentityGroupReviewTests(unittest.TestCase):
    def test_compound_scopes_normalization_blank_rows_and_large_membership(self):
        rows = ((" SAME ", "BE"),) * 105 + (("SAME", "LU"), (None, "BE"), ("", "BE"))
        index = index_matching_identity_keys(
            target_fields=("code", "site"),
            source_keys=rows,
            target_keys=(("SAME", "BE"), ("SAME", "BE"), ("SAME", "LU")),
        )
        result = classify_indexed_matching_identity(
            index,
            dataset_id="products",
            target_model="x.product",
            mode=MappingTargetMode.UPSERT,
            on_existing=None,
        )
        choices = identity_duplicate_choices(
            index,
            dataset_id="products",
            target_model="x.product",
            display_fields=("code", "site"),
            display_keys=rows,
        )
        repeated = [item for item in choices if item.cause.value == "repeated_source"]
        ambiguous = [
            item for item in choices if item.cause.value == "ambiguous_identity_target"
        ]
        self.assertEqual(result.source_repeated_row_count, 105)
        self.assertEqual(result.source_repeated_group_count, 1)
        self.assertEqual(result.source_blank_row_count, 2)
        self.assertEqual(result.target_ambiguous_key_count, 1)
        self.assertEqual(len(repeated), 105)
        self.assertEqual(
            [item.member_position for item in repeated], list(range(1, 106))
        )
        self.assertEqual({item.key for item in choices}, {("SAME", "BE")})
        self.assertEqual(len(ambiguous), 107)
        self.assertEqual(len({item.group_hash for item in choices}), 2)
        self.assertEqual(
            [(item.member_origin, item.member_position) for item in ambiguous[-2:]],
            [("target", 1), ("target", 2)],
        )

    def test_recursive_leaf_paths_do_not_collapse_different_parent_scopes(self):
        rows = (
            ("P1", '["Pump","BE"]'),
            ("P1", '["Pump","BE"]'),
            ("P1", '["Pump","LU"]'),
        )
        leaves = (("P1", "Pump", "BE"), ("P1", "Pump", "BE"), ("P1", "Pump", "LU"))
        index = index_matching_identity_keys(
            target_fields=("ref", "parent_id"), source_keys=rows, target_keys=()
        )
        choices = identity_duplicate_choices(
            index,
            dataset_id="products",
            target_model="x.product",
            display_fields=("ref", "parent_id.name", "parent_id.country_id.code"),
            display_keys=leaves,
        )
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0].related_fields[-1], "parent_id.country_id.code")
        self.assertEqual(choices[0].key, ("P1", "Pump", "BE"))
        payload = {
            name: value
            for name, value in asdict(choices[0]).items()
            if name != "ordinal"
        }
        payload["related_fields"] = list(payload["related_fields"])
        payload["key"] = list(payload["key"])
        self.assertEqual(
            MatchingIdentityChoice.from_protected_dict(payload, ordinal=0), choices[0]
        )
        payload["member_position"] = True
        with self.assertRaises(WorkspaceError):
            MatchingIdentityChoice.from_protected_dict(payload, ordinal=0)

    def test_review_filters_reject_cross_kind_causes_and_invalid_group_receipts(self):
        for options in (
            {"cause": "repeated_source"},
            {"kind": "duplicates", "cause": "missing_target"},
            {"kind": "duplicates", "group_hash": "invalid"},
            {"kind": "unknown"},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises((ValueError, WorkspaceError)),
            ):
                MatchingIdentityChoiceFilter(**options)
