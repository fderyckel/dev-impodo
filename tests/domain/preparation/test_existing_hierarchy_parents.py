"""Prove existing hierarchy levels stay read-only on both supported majors."""

import unittest

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.hierarchy import HierarchyParentPolicy
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import (
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from pathlib import PurePath

HASH = "sha256:" + "1" * 64
MODEL = "x.storage.place"


def plan():
    policy = HierarchyParentPolicy(
        "container_id",
        ("Physical", "Stock"),
        company_field="company_id",
        company_name="Company A",
        fixed_companies=(None, "Company A"),
    )
    return CompiledMigrationPlan(
        plan_id="existing_hierarchy",
        origin="profile_document",
        origin_hash=HASH,
        contract_version=4,
        compiler_version=5,
        datasets=(
            DatasetSpec(
                name="places",
                source=SourceSpec(file="places.csv"),
                target=TargetSpec(model=MODEL),
                hierarchy_parent=policy,
                source_identity=SourceIdentitySpec(fields=("key",)),
                target_identity=TargetIdentitySpec(
                    components=(
                        IdentityComponent(
                            source_fields=("name",), target_fields=("name",)
                        ),
                    ),
                    scope=(
                        IdentityComponent(
                            source_fields=("parent",),
                            target_fields=("container_id",),
                            resolve=ResolveSpec(
                                dataset="places", target_source_fields=("key",)
                            ),
                            null_policy="explicit_scope_null",
                        ),
                        IdentityComponent(
                            source_fields=("company",),
                            target_fields=("company_id",),
                            resolve=ResolveSpec(
                                target_model="res.company", target_fields=("name",)
                            ),
                            null_policy="explicit_scope_null",
                        ),
                    ),
                ),
                fields={"note": FieldSpec(source="note", type="string")},
            ),
        ),
    )


def compare(*, major=19, mode="LOCAL", omit=(), duplicate=False):
    compiled = plan()
    rows = (
        SourceRow(
            2,
            {
                "key": "physical",
                "name": "Physical",
                "parent": None,
                "company": None,
                "note": "Keep Odoo unchanged",
            },
        ),
        SourceRow(
            3,
            {
                "key": "stock",
                "name": "Stock",
                "parent": "physical",
                "company": "Company A",
                "note": "Keep Odoo unchanged",
            },
        ),
        SourceRow(
            4,
            {
                "key": "aisle",
                "name": "A01",
                "parent": "stock",
                "company": "Company A",
                "note": "New aisle",
            },
        ),
    )
    prepared = prepare_source_tables(
        compiled,
        (
            SourceTable(
                "places", PurePath("places.csv"), tuple(rows[0].values), rows, HASH
            ),
        ),
        source_hashes={"places": HASH},
    )
    fields = {
        "name": FieldMetadata("name", "char"),
        "container_id": FieldMetadata("container_id", "many2one", relation=MODEL),
        "company_id": FieldMetadata("company_id", "many2one", relation="res.company"),
        "note": FieldMetadata("note", "char"),
    }
    fingerprint = TargetFingerprint(
        HASH, mode, "fictional", f"{major}.0", "2026-11-12T00:00:00Z"
    )
    metadata = MetadataSnapshot(
        fingerprint,
        {
            MODEL: ModelMetadata(MODEL, "Place", fields),
            "res.company": ModelMetadata(
                "res.company", "Company", {"name": FieldMetadata("name", "char")}
            ),
        },
    )
    places = tuple(
        record
        for record in (
            TargetRecord(
                MODEL,
                1,
                {
                    "name": "Physical",
                    "container_id": False,
                    "company_id": False,
                    "note": "Original root",
                },
            ),
            TargetRecord(
                MODEL,
                2,
                {
                    "name": "Stock",
                    "container_id": 1,
                    "company_id": 10,
                    "note": "Original stock",
                },
            ),
        )
        if record.odoo_id not in omit
    )
    if duplicate:
        places = (*places, TargetRecord(MODEL, 3, dict(places[-1].values)))
    records = RecordSnapshot(
        fingerprint,
        {
            MODEL: places,
            "res.company": (TargetRecord("res.company", 10, {"name": "Company A"}),),
        },
        {MODEL: tuple(fields), "res.company": ("name",)},
    )
    return PreflightEngine().run(compiled, prepared, metadata, records)


class ExistingHierarchyParentTests(unittest.TestCase):
    def test_existing_root_and_second_level_are_never_updated(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    result = compare(major=major, mode=mode)
                    self.assertEqual(
                        {
                            row.source_row: row.classification.value
                            for row in result.decisions
                        },
                        {2: "UNCHANGED", 3: "UNCHANGED", 4: "CREATE"},
                    )
                    self.assertTrue(
                        all(
                            not row.differences
                            for row in result.decisions
                            if row.source_row in {2, 3}
                        )
                    )

    def test_missing_existing_second_level_blocks_its_descendants(self):
        result = compare(omit=(2,))
        decisions = {row.source_row: row for row in result.decisions}
        self.assertEqual(decisions[3].classification.value, "BLOCKED")
        self.assertIn(
            "HIERARCHY_EXISTING_PARENT_NOT_FOUND",
            {issue.code for issue in decisions[3].issues},
        )
        self.assertEqual(decisions[4].classification.value, "BLOCKED")

    def test_ambiguous_existing_second_level_blocks_its_descendants(self):
        decisions = {row.source_row: row for row in compare(duplicate=True).decisions}
        self.assertEqual(decisions[3].classification.value, "AMBIGUOUS")
        self.assertEqual(decisions[4].classification.value, "BLOCKED")

    def test_policy_and_compiled_plan_roundtrip_preserve_the_reference_boundary(self):
        compiled = plan()
        self.assertEqual(
            CompiledMigrationPlan.from_json(compiled.to_json()).semantic_hash,
            compiled.semantic_hash,
        )
        policy = compiled.datasets[0].hierarchy_parent
        self.assertEqual(HierarchyParentPolicy.from_dict(policy.to_dict()), policy)
        with self.assertRaisesRegex(ValueError, "compiled plan v4"):
            CompiledMigrationPlan(
                **{
                    **compiled.model_dump(),
                    "contract_version": 3,
                    "compiler_version": 4,
                }
            )
