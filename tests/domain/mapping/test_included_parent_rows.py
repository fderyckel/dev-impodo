"""Verify ordered incoming keys, parent rule chains, and unsafe bindings."""

from dataclasses import replace
from unittest import TestCase
from uuid import uuid4

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipResolver,
    ResolverOrigin,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.domain.mapping.row_inclusion import (
    IncludedParentRuleError,
    compile_included_parent_rows,
    row_is_included,
)


def child_mapping(name, parent, parent_columns, *, identity=("id",)):
    return DatasetMapping(
        name, f"x.{name}",
        source_identity_column_keys=identity,
        target_scope=(IdentityComponentMapping(
            parent_columns, ("parent_id",),
            resolver=RelationshipResolver(ResolverOrigin.DATASET, dataset_id=parent),
        ),),
        row_inclusion=RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS, included_parent_field="parent_id",
        ),
    )


class IncludedParentScopeTests(TestCase):
    def test_composite_scope_follows_two_generations_and_keeps_key_order(self):
        orders = DatasetMapping(
            "orders", "x.order", source_identity_column_keys=("site", "id"),
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(RowInclusionCondition(
                    str(uuid4()), "status", SelectionConditionOperator.EQUALS, "Active",
                ),),
            ),
        )
        lines = child_mapping("lines", "orders", ("warehouse", "order"))
        details = child_mapping("details", "lines", ("line",))
        sources = {
            "orders": (
                {"site": "A", "id": "01", "status": "Active"},
                {"site": "B", "id": "01", "status": "Inactive"},
            ),
            "lines": (
                {"id": "L1", "warehouse": " A ", "order": "01"},
                {"id": "L2", "warehouse": "B", "order": "01"},
                {"id": "L3", "warehouse": "A", "order": ""},
            ),
        }
        reads = []

        def read(dataset_id, keys):
            reads.append((dataset_id, keys))
            return sources[dataset_id]

        gates = compile_included_parent_rows(
            (details, lines, orders),
            {
                "orders": frozenset(("site", "id", "status")),
                "lines": frozenset(("id", "warehouse", "order")),
                "details": frozenset(("id", "line")),
            },
            read,
        )
        self.assertEqual(reads, [
            ("orders", ("site", "id", "status")),
            ("lines", ("id", "warehouse", "order")),
        ])
        self.assertEqual(gates["lines"].keys, frozenset({("A", "01")}))
        self.assertEqual(gates["details"].keys, frozenset({("L1",)}))
        for row, expected in zip(sources["lines"], (True, False, False), strict=True):
            self.assertEqual(row_is_included(lines.row_inclusion, row, gates["lines"]), expected)
        self.assertFalse(row_is_included(
            details.row_inclusion, {"line": "absent"}, gates["details"],
        ))

    def test_duplicate_parent_identifiers_are_reported_and_cycle_reads_nothing(self):
        child = child_mapping("child", "parent", ("parent",))
        parent = DatasetMapping("parent", "x.parent", source_identity_column_keys=("id",))
        columns = {name: frozenset(("id", "parent")) for name in ("parent", "child")}
        gates = compile_included_parent_rows(
            (child, parent), columns,
            lambda _name, _keys: ({"id": "1"}, {"id": " 1 "}),
        )
        self.assertEqual(gates["child"].duplicate_key_count, 1)
        loop = child_mapping("parent", "child", ("parent",))
        with self.assertRaisesRegex(IncludedParentRuleError, "circular parent"):
            compile_included_parent_rows(
                (child, loop), columns,
                lambda *_args: self.fail("a circular rule cannot read source data"),
            )
        invalid = replace(child, row_inclusion=RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS, included_parent_field="removed",
        ))
        with self.assertRaisesRegex(IncludedParentRuleError, "mapped incoming parent"):
            compile_included_parent_rows(
                (invalid, parent), columns,
                lambda *_args: self.fail("a missing link cannot read source data"),
            )
