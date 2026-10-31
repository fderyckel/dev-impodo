"""Discover generic hierarchy consumers without changing accepted source rules."""

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    StructuralOutputColumn,
    StructuralProjection,
    structural_dataset_id,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    derived_consumer_links,
    derived_dataset_links,
    hierarchy_consumer_bindings,
    hierarchy_consumer_values,
)


def join(left, right, name):
    return ExactJoinRule(
        str(uuid4()),
        name,
        left,
        right,
        (JoinKey("id", "id"),),
        (StructuralOutputColumn("id", "ID", "string"),),
        (StructuralProjection("id", left, "id"),),
    )


def hierarchy(source, name):
    return HierarchicalLookupRule(
        str(uuid4()),
        name,
        source,
        ("department", "team"),
        "x_department",
        "display_name",
        "fictional_org",
        HierarchyValuePolicy("promote"),
        all_blank=HierarchyValuePolicy("fixed", "Unassigned"),
    )


class CombinedHierarchyBindingTests(unittest.TestCase):
    def test_pairwise_joins_carry_each_hierarchy_to_only_its_descendants(self):
        first = join("alpha", "bravo", "combined_ab")
        second = join(structural_dataset_id(first), "charlie", "combined_abc")
        other = join("delta", "echo", "combined_de")
        rules = tuple(
            hierarchy(source, f"teams_{source}")
            for source in ("alpha", "bravo", "charlie", "delta")
        )
        plan = DerivedEntityPlan(
            str(uuid4()),
            1,
            str(uuid4()),
            "sha256:" + "a" * 64,
            (*rules, first, second, other),
            datetime.now(UTC),
            "Fictional operator",
        )
        before = plan.content_hash
        bindings = hierarchy_consumer_bindings(plan)
        consumers = {
            rule.source_dataset_id: {
                binding.link.consumer_dataset_id
                for binding in bindings
                if binding.rule.rule_id == rule.rule_id and binding.carried
            }
            for rule in rules
        }
        self.assertEqual(
            consumers,
            {
                "alpha": {structural_dataset_id(first), structural_dataset_id(second)},
                "bravo": {structural_dataset_id(first), structural_dataset_id(second)},
                "charlie": {structural_dataset_id(second)},
                "delta": {structural_dataset_id(other)},
            },
        )
        self.assertEqual(len(derived_dataset_links(plan)), 4)
        self.assertEqual(len(derived_consumer_links(plan)), 10)
        self.assertEqual(plan.content_hash, before)
        binding = next(item for item in bindings if item.carried)
        unmatched = hierarchy_consumer_values(binding, None)
        blank = hierarchy_consumer_values(binding, {"department": "", "team": ""})
        self.assertIsNone(unmatched[binding.link.source_column_key])
        self.assertFalse(unmatched[binding.presence_key])
        self.assertEqual(blank[binding.link.source_column_key], "unassigned")
        self.assertTrue(blank[binding.presence_key])
        raw = {"department": " Sales ", "team": "Service"}
        projected = hierarchy_consumer_values(binding, raw)
        self.assertEqual(projected[binding.link.source_column_key], "sales / service")
        self.assertEqual(
            [projected[key] for key in binding.link.source_level_column_keys],
            list(raw.values()),
        )
