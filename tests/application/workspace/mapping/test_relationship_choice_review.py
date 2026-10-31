"""Prove review causes come from the existing lookup and source-choice rules."""

import unittest
from collections import Counter

from impodo.application.workspace.mapping.order_service import (
    RelationshipHealthLiveProbe,
    _classify_relationship_probe,
    _relationship_source_key_counts,
)
from impodo.domain.mapping.contracts import (
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.relationship_review import RelationshipChoice
from tests.application.workspace.mapping import (
    test_matching_order_live_check as fixtures,
)
from impodo.domain.shared.models import TargetRecord


class RelationshipChoiceReviewTests(unittest.TestCase):
    def probe(self, origin, *, required=False, kind="many2one", rows=None, mappings=()):
        relationship = RelationshipMapping(
            "product_tmpl_id",
            kind,
            ("column:article",),
            resolver=RelationshipResolver(
                origin=origin,
                model="product.template",
                dataset_id="dataset:article"
                if origin is not ResolverOrigin.TARGET_CATALOG
                else None,
                value_mappings=mappings,
            ),
            required=required,
        )
        incomplete, affected = Counter(), Counter()
        source, blanks, partial = _relationship_source_key_counts(
            relationship,
            rows or (),
            target_fields=("ref",),
            incomplete_keys=incomplete,
            affected_rows=affected,
        )
        return RelationshipHealthLiveProbe(
            "dataset:bom",
            relationship.resolver.dataset_id,
            "product_tmpl_id",
            kind,
            origin,
            required,
            "product.template",
            ("ref",),
            len(rows or ()),
            tuple(source.items()),
            blanks,
            partial,
            ((("INCOMING",), 1), (("DUP-IN",), 2), (("Case Incoming",), 1)),
            incomplete_key_counts=tuple(incomplete.items()),
            source_key_row_counts=tuple(affected.items()),
        )

    def test_all_resolver_origins_and_target_first_failure_precedence(self):
        evidence = fixtures._evidence()
        records = tuple(
            TargetRecord("product.template", 900001 + index, {"ref": value})
            for index, value in enumerate(("TARGET", "DUP", "DUP", "Case Target"))
        )
        _metadata, snapshot = fixtures._product_snapshot(evidence, records=records)
        rows = tuple(
            (value,)
            for value in (
                "TARGET",
                "DUP",
                "case target",
                "INCOMING",
                "DUP-IN",
                "case incoming",
                "MISSING",
            )
        )
        for origin in ResolverOrigin:
            with self.subTest(origin=origin):
                choices = []
                result = _classify_relationship_probe(
                    self.probe(origin, rows=rows), snapshot, choices=choices
                )
                self.assertEqual(
                    sum(item.occurrence_count for item in choices), result.blocked_count
                )
                self.assertTrue(all(item.resolver_origin is origin for item in choices))
                self.assertNotIn("900001", str(choices))
        probe = self.probe(ResolverOrigin.TARGET_THEN_DATASET, rows=(("case target",),))
        from dataclasses import replace

        probe = replace(probe, incoming_key_counts=((("case target",), 1),))
        choices = []
        result = _classify_relationship_probe(probe, snapshot, choices=choices)
        self.assertEqual(result.incoming_count, 0)
        self.assertEqual(choices[0].cause.value, "case_only")
        self.assertEqual(choices[0].match_origin, "target")

    def test_partial_keys_required_blanks_and_optional_blanks(self):
        evidence = fixtures._evidence()
        _metadata, snapshot = fixtures._product_snapshot(evidence, records=())
        for required in (True, False):
            choices = []
            result = _classify_relationship_probe(
                self.probe(
                    ResolverOrigin.TARGET_CATALOG,
                    required=required,
                    rows=((None,), ("  ",), ("MISSING",)),
                ),
                snapshot,
                choices=choices,
            )
            self.assertEqual(result.blank_row_count, 2)
            self.assertEqual(
                sum(row.occurrence_count for row in choices), 3 if required else 1
            )
        relationship = RelationshipMapping(
            "parent_id",
            "many2one",
            ("a", "b"),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG, model="product.template"
            ),
        )
        incomplete, affected = Counter(), Counter()
        source, blank, partial = _relationship_source_key_counts(
            relationship,
            ((None, "BE"), ("  ", "LU"), (None, None)),
            target_fields=("ref", "scope"),
            incomplete_keys=incomplete,
            affected_rows=affected,
        )
        probe = RelationshipHealthLiveProbe(
            "dataset:bom",
            None,
            "parent_id",
            "many2one",
            ResolverOrigin.TARGET_CATALOG,
            False,
            "product.template",
            ("ref", "scope"),
            3,
            tuple(source.items()),
            blank,
            partial,
            (),
            incomplete_key_counts=tuple(incomplete.items()),
        )
        choices = []
        _classify_relationship_probe(probe, snapshot, choices=choices)
        self.assertEqual({row.key for row in choices}, {(None, "BE"), (None, "LU")})
        self.assertTrue(all(row.cause.value == "incomplete" for row in choices))

    def test_repeated_many2many_members_and_translated_aliases_preserve_row_impact(
        self,
    ):
        evidence = fixtures._evidence()
        _metadata, snapshot = fixtures._product_snapshot(evidence, records=())
        probe = self.probe(
            ResolverOrigin.TARGET_CATALOG,
            kind="many2many",
            rows=(("A; A; B",),),
            mappings=(ValueMapping("A", "SAME"), ValueMapping("B", "SAME")),
        )
        choices = []
        result = _classify_relationship_probe(probe, snapshot, choices=choices)
        self.assertEqual(result.missing_count, 2)
        self.assertEqual(len(choices), 1)
        self.assertEqual(
            (choices[0].occurrence_count, choices[0].affected_row_count), (2, 1)
        )
        self.assertEqual(
            RelationshipChoice.from_dict(choices[0].protected_dict()), choices[0]
        )
