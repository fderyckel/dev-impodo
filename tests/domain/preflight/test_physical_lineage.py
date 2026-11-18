"""Prove exact saved physical coordinates without evaluating source data."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4

from impodo.domain.preflight.physical_lineage import PhysicalColumnIndex
from impodo.domain.preparation.staging_contracts import CanonicalLineage
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.structural import (
    AggregateOperation,
    AggregateSpec,
    ExactJoinRule,
    GroupAggregateRule,
    GroupKey,
    JoinKey,
    StructuralOutputColumn,
    StructuralProjection,
    UnionAllRule,
    UnionBranch,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    RelatedDatasetRule,
    SourcePreparationRule,
    derived_dataset_links,
    mapping_source_selection,
    related_dataset_links,
)

_HASH = "sha256:" + "1" * 64
_NOW = datetime(2026, 11, 18, tzinfo=UTC)


def _dataset(name: str) -> SourceDataset:
    return SourceDataset(
        dataset_id=name,
        name=name.title(),
        source=FileSourceBinding(name, f"{name}.csv", _HASH, _HASH, "utf-8", ",", 1),
        row_count=20,
        columns=tuple(
            SourceDatasetColumn(index, key.title(), f"{name}:{key}", "string")
            for index, key in enumerate(("key", "value"), 1)
        ),
    )


def _selection() -> SourceSelection:
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=_NOW,
        created_by="reviewer",
        content_hash=_HASH,
        datasets=tuple(_dataset(name) for name in ("left", "right", "third")),
    )


def _plan(
    selection: SourceSelection, *rules: SourcePreparationRule
) -> DerivedEntityPlan:
    return DerivedEntityPlan(
        str(uuid4()),
        1,
        str(uuid4()),
        selection.content_hash,
        tuple(rules),
        _NOW,
        "reviewer",
    )


def _join(left: str = "left", right: str = "right", *, name: str = "joined"):
    return ExactJoinRule(
        rule_id=str(uuid4()),
        output_dataset_name=name,
        left_dataset_id=left,
        right_dataset_id=right,
        keys=(JoinKey(f"{left}:key", f"{right}:key"),),
        output_columns=(
            StructuralOutputColumn("joined:key", "Key", "string"),
            StructuralOutputColumn("joined:left", "Left value", "string"),
            StructuralOutputColumn("joined:right", "Right value", "string"),
        ),
        projections=(
            StructuralProjection("joined:key", left, f"{left}:key"),
            StructuralProjection("joined:left", left, f"{left}:value"),
            StructuralProjection("joined:right", right, f"{right}:value"),
        ),
    )


def _union() -> UnionAllRule:
    return UnionAllRule(
        str(uuid4()),
        "united",
        (StructuralOutputColumn("united:value", "Value", "string"),),
        tuple(
            UnionBranch(
                name, (StructuralProjection("united:value", name, f"{name}:value"),)
            )
            for name in ("left", "right")
        ),
    )


def _group(source: str, key: str, value: str) -> GroupAggregateRule:
    return GroupAggregateRule(
        str(uuid4()),
        "grouped",
        source,
        (
            StructuralOutputColumn("group:key", "Key", "string"),
            StructuralOutputColumn("group:sum", "Sum", "decimal"),
            StructuralOutputColumn("group:count", "Count", "integer"),
        ),
        (GroupKey("group:key", key),),
        (
            AggregateSpec("group:sum", AggregateOperation.SUM, value),
            AggregateSpec("group:count", AggregateOperation.COUNT),
        ),
    )


def _lineage(
    selection: SourceSelection,
    dataset_id: str,
    rows: dict[str, tuple[int, ...]],
    plan: DerivedEntityPlan | None,
):
    primary = next(iter(rows))
    return CanonicalLineage(
        source_selection_hash=selection.content_hash,
        source_hash=_HASH,
        mapping_hash=_HASH,
        schema_hash=_HASH,
        derived_plan_hash=plan.content_hash if plan else None,
        dataset=next(
            item.name for item in selection.datasets if item.dataset_id == dataset_id
        ),
        source_row=1,
        physical_dataset_id=primary,
        physical_source_rows=rows[primary],
        physical_sources=rows,
        field_sources={},
    )


def _cells(cells):
    return (
        None
        if cells is None
        else tuple(
            (cell.dataset_id, cell.source_row, cell.source_key) for cell in cells
        )
    )


class PhysicalColumnIndexTests(TestCase):
    def setUp(self):
        self.physical = _selection()
        self.catalogs = tuple(
            SimpleNamespace(
                file_id=item.dataset_id,
                source_sha256=_HASH,
                content_hash=_HASH,
                tables=(
                    SimpleNamespace(
                        table_key=f"{item.dataset_id}.csv",
                        row_count=20,
                        preview_rows=(("Group", "Leaf"),),
                        columns=tuple(
                            SimpleNamespace(
                                ordinal=column.ordinal,
                                distinct_count=1,
                                distinct_count_is_exact=True,
                            )
                            for column in item.columns
                        ),
                    ),
                ),
            )
            for item in self.physical.datasets
        )

    def test_direct_cells_preserve_input_order_and_deduplicate(self):
        index = PhysicalColumnIndex(self.physical, self.physical, None)
        keys = ("left:value", "left:key", "left:value")
        expected = (("left", 9, "left:value"), ("left", 9, "left:key"))
        self.assertEqual(_cells(index.resolve_direct("left", keys, 9)), expected)
        lineage = _lineage(self.physical, "left", {"left": (9,)}, None)
        self.assertEqual(_cells(index.resolve("left", keys, lineage)), expected)
        self.assertIsNone(index.resolve_direct("left", ("synthetic",), 9))
        self.assertIsNone(index.resolve_direct("left", keys, True))
        self.assertIsNone(index.resolve_direct("left", keys, 0))
        self.assertEqual(index.resolve("left", (), lineage), ())

    def test_join_uses_saved_physical_rows_and_requires_the_whole_group(self):
        rule = _join()
        plan = _plan(self.physical, rule)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        output = structural_dataset_id(rule)
        index = PhysicalColumnIndex(selection, self.physical, plan)
        keys = ("joined:left", "joined:right")
        lineage = _lineage(selection, output, {"left": (9,), "right": (3,)}, plan)
        self.assertEqual(
            _cells(index.resolve(output, keys, lineage)),
            (
                ("left", 9, "left:value"),
                ("right", 3, "right:value"),
            ),
        )
        outer = _lineage(selection, output, {"left": (9,)}, plan)
        self.assertIsNone(index.resolve(output, keys, outer))
        self.assertEqual(
            _cells(index.resolve(output, (keys[0],), outer)),
            (("left", 9, "left:value"),),
        )
        self.assertIsNone(index.resolve_direct(output, keys, 1))

    def test_chained_join_resolves_original_columns_through_aliases(self):
        first = _join()
        source = structural_dataset_id(first)
        second = ExactJoinRule(
            str(uuid4()),
            "chained",
            source,
            "third",
            (JoinKey("joined:key", "third:key"),),
            (StructuralOutputColumn("chained:value", "Value", "string"),),
            (StructuralProjection("chained:value", source, "joined:right"),),
        )
        plan = _plan(self.physical, first, second)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        output = structural_dataset_id(second)
        lineage = _lineage(
            selection, output, {"left": (9,), "right": (3,), "third": (7,)}, plan
        )
        self.assertEqual(
            _cells(
                PhysicalColumnIndex(selection, self.physical, plan).resolve(
                    output,
                    ("chained:value",),
                    lineage,
                )
            ),
            (("right", 3, "right:value"),),
        )

    def test_union_selects_one_saved_branch_and_rejects_ambiguous_cohorts(self):
        rule = _union()
        plan = _plan(self.physical, rule)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        output = structural_dataset_id(rule)
        index = PhysicalColumnIndex(selection, self.physical, plan)
        for name, row in (("left", 9), ("right", 3)):
            lineage = _lineage(selection, output, {name: (row,)}, plan)
            self.assertEqual(
                _cells(index.resolve(output, ("united:value",), lineage)),
                ((name, row, f"{name}:value"),),
            )
        ambiguous = _lineage(selection, output, {"left": (9,), "right": (3,)}, plan)
        self.assertIsNone(index.resolve(output, ("united:value",), ambiguous))

    def test_reused_join_role_is_unproven_but_independent_columns_remain_provable(self):
        first = _join()
        source = structural_dataset_id(first)
        second = ExactJoinRule(
            str(uuid4()),
            "reused",
            source,
            "left",
            (JoinKey("joined:key", "left:key"),),
            (
                StructuralOutputColumn("reused:left", "Left", "string"),
                StructuralOutputColumn("reused:right", "Right", "string"),
            ),
            (
                StructuralProjection("reused:left", source, "joined:left"),
                StructuralProjection("reused:right", source, "joined:right"),
            ),
        )
        plan = _plan(self.physical, first, second)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        output = structural_dataset_id(second)
        lineage = _lineage(selection, output, {"left": (2, 9), "right": (3,)}, plan)
        index = PhysicalColumnIndex(selection, self.physical, plan)
        self.assertIsNone(index.resolve(output, ("reused:left",), lineage))
        self.assertEqual(
            _cells(index.resolve(output, ("reused:right",), lineage)),
            (("right", 3, "right:value"),),
        )
        self.assertIsNone(
            index.resolve(output, ("reused:right", "reused:left"), lineage)
        )

    def test_grouped_inputs_include_all_member_cells_and_count_has_no_cell(self):
        rule = _group("left", "left:key", "left:value")
        plan = _plan(self.physical, rule)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        output = structural_dataset_id(rule)
        index = PhysicalColumnIndex(selection, self.physical, plan)
        lineage = _lineage(selection, output, {"left": (2, 9)}, plan)
        self.assertEqual(
            _cells(index.resolve(output, ("group:sum", "group:key"), lineage)),
            (
                ("left", 2, "left:value"),
                ("left", 9, "left:value"),
                ("left", 2, "left:key"),
                ("left", 9, "left:key"),
            ),
        )
        self.assertEqual(index.resolve(output, ("group:count",), lineage), ())

    def test_group_after_union_accepts_disjoint_branch_members(self):
        union = _union()
        group = _group(structural_dataset_id(union), "united:value", "united:value")
        plan = _plan(self.physical, union, group)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        output = structural_dataset_id(group)
        lineage = _lineage(selection, output, {"left": (2, 9), "right": (3,)}, plan)
        self.assertEqual(
            _cells(
                PhysicalColumnIndex(selection, self.physical, plan).resolve(
                    output,
                    ("group:sum",),
                    lineage,
                )
            ),
            (
                ("left", 2, "left:value"),
                ("left", 9, "left:value"),
                ("right", 3, "right:value"),
            ),
        )

    def test_related_parent_groups_keys_and_child_retains_its_single_row(self):
        rule = RelatedDatasetRule(
            str(uuid4()), "left", "parents", "children", "left:key", "left:value"
        )
        plan = _plan(self.physical, rule)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        link = related_dataset_links(plan)[0]
        index = PhysicalColumnIndex(selection, self.physical, plan)
        parent = _lineage(selection, link.parent_dataset_id, {"left": (2, 9)}, plan)
        self.assertEqual(
            _cells(index.resolve(link.parent_dataset_id, ("left:key",), parent)),
            (("left", 2, "left:key"), ("left", 9, "left:key")),
        )
        child = _lineage(selection, link.child_dataset_id, {"left": (9,)}, plan)
        self.assertEqual(
            _cells(index.resolve(link.child_dataset_id, ("left:value",), child)),
            (("left", 9, "left:value"),),
        )

    def test_extracted_lookup_uses_all_original_cells_and_deduplicates_shared_origins(
        self,
    ):
        rule = DerivedEntityRule(
            str(uuid4()), "lookup", "left", "left:value", "x.lookup", "name", "lookup"
        )
        plan = _plan(self.physical, rule)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        link = derived_dataset_links(plan)[0]
        lineage = _lineage(selection, link.derived_dataset_id, {"left": (2, 9)}, plan)
        self.assertEqual(
            _cells(
                PhysicalColumnIndex(selection, self.physical, plan).resolve(
                    link.derived_dataset_id,
                    (link.name_column_key, link.canonical_key_column_key),
                    lineage,
                )
            ),
            (("left", 2, "left:value"), ("left", 9, "left:value")),
        )

    def test_hierarchy_consumer_inputs_are_provable_but_ancestor_level_is_not_guessed(
        self,
    ):
        rule = HierarchicalLookupRule(
            str(uuid4()),
            "hierarchy",
            "left",
            ("left:key", "left:value"),
            "x.lookup",
            "name",
            "hierarchy",
            HierarchyValuePolicy("promote"),
        )
        plan = _plan(self.physical, rule)
        selection = mapping_source_selection(self.physical, plan, self.catalogs)
        link = derived_dataset_links(plan)[0]
        index = PhysicalColumnIndex(selection, self.physical, plan)
        consumer = _lineage(selection, "left", {"left": (9,)}, plan)
        self.assertEqual(
            _cells(index.resolve("left", (link.source_column_key,), consumer)),
            (("left", 9, "left:key"), ("left", 9, "left:value")),
        )
        ancestor = _lineage(selection, link.derived_dataset_id, {"left": (2, 9)}, plan)
        self.assertIsNone(
            index.resolve(link.derived_dataset_id, (link.name_column_key,), ancestor)
        )

    def test_stale_lineage_and_changed_or_duplicate_source_metadata_are_rejected(self):
        index = PhysicalColumnIndex(self.physical, self.physical, None)
        lineage = _lineage(self.physical, "left", {"left": (9,)}, None)
        self.assertIsNone(
            index.resolve(
                "left",
                ("left:value",),
                replace(
                    lineage,
                    source_selection_hash="sha256:" + "2" * 64,
                ),
            )
        )
        with self.assertRaises(ValueError):
            PhysicalColumnIndex(
                self.physical,
                self.physical,
                replace(
                    _plan(self.physical),
                    source_selection_hash="sha256:" + "2" * 64,
                ),
            )
        source = self.physical.datasets[0]
        for columns in (
            (*source.columns, replace(source.columns[0], ordinal=3)),
            tuple(reversed(source.columns)),
        ):
            changed = replace(
                self.physical,
                datasets=(
                    replace(source, columns=columns),
                    *self.physical.datasets[1:],
                ),
            )
            with self.assertRaises(ValueError):
                PhysicalColumnIndex(changed, self.physical, None)
