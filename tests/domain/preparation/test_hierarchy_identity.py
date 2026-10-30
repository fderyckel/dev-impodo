"""Keep complete hierarchy identity paths portable, bounded, and fail closed."""

import unittest
from dataclasses import replace
from pathlib import PurePath

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.execution.planner import plan_record_requests
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import (
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    IdentityComponent,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    BusinessReference,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    portable_value,
    restore_portable_value,
)

HASH = "sha256:" + "1" * 64


def hierarchy_plan(*, compound=False):
    return CompiledMigrationPlan(
        plan_id="fictional_hierarchy",
        origin="profile_document",
        origin_hash=HASH,
        datasets=(
            DatasetSpec(
                name="categories",
                source=SourceSpec(file="categories.csv"),
                target=TargetSpec(model="product.category", mode="upsert"),
                source_identity=SourceIdentitySpec(
                    fields=("key", "partition") if compound else ("key",)
                ),
                target_identity=TargetIdentitySpec(
                    components=(
                        IdentityComponent(
                            source_fields=("name",), target_fields=("name",)
                        ),
                    ),
                    scope=(
                        IdentityComponent(
                            source_fields=("parent", "parent_partition")
                            if compound
                            else ("parent",),
                            target_fields=("parent_id",),
                            resolve=ResolveSpec(
                                dataset="categories",
                                target_source_fields=("key", "partition")
                                if compound
                                else ("key",),
                            ),
                            null_policy="explicit_scope_null",
                        ),
                    ),
                ),
            ),
        ),
    )


def prepare(plan, values):
    rows = tuple(SourceRow(index, dict(value)) for index, value in enumerate(values, 2))
    return prepare_source_tables(
        plan,
        (
            SourceTable(
                "categories", PurePath("categories.csv"), tuple(values[0]), rows, HASH
            ),
        ),
        source_hashes={"categories": HASH},
    )


def compare(plan, prepared, targets=()):
    fingerprint = TargetFingerprint(
        HASH, "LOCAL", "fictional_odoo19", "19.0", "2026-10-30T00:00:00Z"
    )
    metadata = MetadataSnapshot(
        fingerprint,
        {
            "product.category": ModelMetadata(
                "product.category",
                "Category",
                {
                    "name": FieldMetadata("name", "char"),
                    "parent_id": FieldMetadata(
                        "parent_id", "many2one", relation="product.category"
                    ),
                },
            )
        },
    )
    records = RecordSnapshot(
        fingerprint,
        {"product.category": tuple(targets)},
        {"product.category": ("name", "parent_id")},
    )
    return PreflightEngine().run(plan, prepared, metadata, records)


class HierarchyIdentityTests(unittest.TestCase):
    def test_incoming_identity_cycle_becomes_saved_blocker(self):
        plan = hierarchy_plan()
        prepared = prepare(
            plan,
            (
                {"key": "A", "name": "First", "parent": "B"},
                {"key": "B", "name": "Second", "parent": "A"},
            ),
        )
        result = compare(plan, prepared)
        self.assertEqual(
            {row.classification.value for row in result.decisions}, {"BLOCKED"}
        )
        self.assertIn(
            "REFERENCE_IDENTITY_CYCLE", {issue.code for issue in result.issues}
        )

    def test_target_parent_cycle_becomes_comparison_blocker(self):
        plan = hierarchy_plan()
        prepared = prepare(plan, ({"key": "ROOT", "name": "Machines", "parent": ""},))
        result = compare(
            plan,
            prepared,
            (
                TargetRecord("product.category", 1, {"name": "Machines", "parent_id": 2}),
                TargetRecord("product.category", 2, {"name": "Loop", "parent_id": 1}),
            ),
        )
        self.assertEqual(result.decisions[0].classification.value, "BLOCKED")
        self.assertIn(
            "TARGET_REFERENCE_UNRESOLVED", {issue.code for issue in result.issues}
        )

    def test_ambiguous_root_is_not_selected_arbitrarily(self):
        plan = hierarchy_plan()
        prepared = prepare(plan, ({"key": "ROOT", "name": "Machines", "parent": ""},))
        result = compare(
            plan,
            prepared,
            tuple(
                TargetRecord(
                    "product.category",
                    identifier,
                    {"name": "Machines", "parent_id": False},
                )
                for identifier in (1, 2)
            ),
        )
        self.assertEqual(result.decisions[0].classification.value, "AMBIGUOUS")
        self.assertEqual(result.decisions[0].target_match_count, 2)

    def test_missing_parent_remains_a_blocker(self):
        plan = hierarchy_plan()
        result = compare(
            plan, prepare(plan, ({"key": "CHILD", "name": "Seals", "parent": "MISSING"},))
        )
        self.assertEqual(result.decisions[0].classification.value, "BLOCKED")
        self.assertIn("REFERENCE_NOT_FOUND", {issue.code for issue in result.issues})

    def test_partly_blank_compound_parent_never_becomes_a_root(self):
        plan = hierarchy_plan(compound=True)
        prepared = prepare(
            plan,
            (
                {
                    "key": "ROOT",
                    "partition": "A",
                    "name": "Machines",
                    "parent": "",
                    "parent_partition": "",
                },
                {
                    "key": "CHILD",
                    "partition": "A",
                    "name": "Seals",
                    "parent": "ROOT",
                    "parent_partition": "",
                },
            ),
        )
        self.assertEqual(prepared.records[0].target_scope, (None,))
        self.assertTrue(prepared.records[1].blocked)
        self.assertNotEqual(prepared.records[1].target_scope, (None,))

    def test_lineage_requests_remain_bounded_and_order_independent(self):
        plan = hierarchy_plan()
        values = tuple(
            {"key": f"R{index}", "name": f"Root {index}", "parent": ""}
            for index in range(1001)
        )
        first = plan_record_requests(plan, prepare(plan, values).records)
        second = plan_record_requests(
            plan, prepare(plan, tuple(reversed(values))).records
        )
        self.assertEqual(len(first), 3)
        self.assertEqual(first, second)
        self.assertTrue(all(request.domain for request in first))
        self.assertTrue(
            all(sum(term == "|" for term in request.domain) < 500 for request in first)
        )

    def test_historical_reference_bytes_and_nested_reference_round_trip(self):
        historic = BusinessReference("product.category", ("Machines",), (None,))
        self.assertEqual(
            portable_value(historic),
            {"model": "product.category", "key": ["Machines"], "scope": [None]},
        )
        root = replace(
            historic, target_fields=("name",), target_scope_fields=("parent_id",)
        )
        child = BusinessReference(
            "product.category",
            ("Pumps",),
            (root,),
            target_fields=("name",),
            target_scope_fields=("parent_id",),
        )
        self.assertEqual(restore_portable_value(portable_value(child)), child)
