"""Keep update-only mappings portable and prevent creation of missing records."""

import unittest
from dataclasses import replace
from pathlib import PurePath
from types import SimpleNamespace

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.execution_snapshot import build_execution_snapshot
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    MappingTargetMode,
)
from impodo.domain.matching_order import classify_matching_identity
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
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    Classification,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)

HASH = "sha256:" + "1" * 64


class UpdateOnlyContractTests(unittest.TestCase):
    def test_new_modes_require_their_own_mapping_and_compiler_contracts(self):
        definition = MappingDefinition(
            "updates",
            HASH,
            HASH,
            (DatasetMapping("records", "x.asset", mode=MappingTargetMode.UPDATE),),
        )
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        with self.assertRaisesRegex(ValueError, "v24"):
            replace(definition, contract_version=23)
        legacy = replace(
            definition,
            contract_version=23,
            datasets=(
                replace(
                    definition.datasets[0],
                    mode=MappingTargetMode.UPSERT,
                ),
            ),
        )
        self.assertEqual(MappingDefinition.from_json(legacy.to_json()), legacy)
        with self.assertRaisesRegex(ValueError, "v3"):
            self.plan("x.asset", contract_version=2, compiler_version=3)

    def test_existing_archived_missing_and_ambiguous_rows_never_create(self):
        for major in (19, 20):
            for model in ("account.account", "x.asset"):
                with self.subTest(major=major, model=model):
                    plan = self.plan(model)
                    table = SourceTable(
                        "records",
                        PurePath("records.csv"),
                        ("code", "active"),
                        (
                            SourceRow(2, {"code": "A", "active": "False"}),
                            SourceRow(3, {"code": "B", "active": "False"}),
                            SourceRow(4, {"code": "MISSING", "active": "False"}),
                            SourceRow(5, {"code": "DUPLICATE", "active": "False"}),
                        ),
                        HASH,
                    )
                    prepared = prepare_source_tables(
                        plan, (table,), source_hashes={"records": HASH}
                    )
                    fingerprint = TargetFingerprint(
                        HASH,
                        "REMOTE",
                        "fictional",
                        f"{major}.0",
                        "2026-11-10T10:00:00Z",
                        {},
                    )
                    metadata = MetadataSnapshot(
                        fingerprint,
                        {
                            model: ModelMetadata(
                                model,
                                "Records",
                                {
                                    "code": FieldMetadata("code", "char"),
                                    "name": FieldMetadata(
                                        "name", "char", required=True
                                    ),
                                    "active": FieldMetadata("active", "boolean"),
                                },
                            )
                        },
                    )
                    rows = tuple(
                        TargetRecord(model, index, {"code": code, "active": active})
                        for index, (code, active) in enumerate(
                            (
                                ("A", True),
                                ("B", False),
                                ("DUPLICATE", True),
                                ("DUPLICATE", True),
                            ),
                            1,
                        )
                    )
                    report = PreflightEngine().run(
                        plan,
                        prepared,
                        metadata,
                        RecordSnapshot(
                            fingerprint, {model: rows}, {model: ("code", "active")}
                        ),
                    )
                    self.assertEqual(
                        [row.classification for row in report.decisions],
                        [
                            Classification.UPDATE,
                            Classification.UNCHANGED,
                            Classification.AMBIGUOUS,
                            Classification.BLOCKED,
                        ],
                    )
                    self.assertNotIn(
                        Classification.CREATE,
                        [row.classification for row in report.decisions],
                    )
                    self.assertEqual(report.decisions[0].differences[0].field, "active")
                    self.assertIn(
                        "UPDATE_TARGET_NOT_FOUND",
                        {
                            issue.code
                            for row in report.decisions
                            for issue in row.issues
                        },
                    )
                    with self.assertRaisesRegex(ValueError, "cannot create"):
                        build_execution_snapshot(
                            preflight_run_id="forged-create",
                            frozen=SimpleNamespace(plan=plan, prepared=prepared),
                            result=replace(
                                report,
                                decisions=(
                                    replace(
                                        report.decisions[0],
                                        classification=Classification.CREATE,
                                    ),
                                ),
                                metadata_snapshot_hash=HASH,
                                record_snapshot_hash=HASH,
                            ),
                        )

    def test_identity_check_blocks_missing_update_rows_without_creating(self):
        result = classify_matching_identity(
            dataset_id="records",
            target_model="x.asset",
            target_fields=("code",),
            source_keys=(("A",), ("MISSING",)),
            target_keys=(("A",),),
            mode=MappingTargetMode.UPDATE,
            on_existing=None,
        )
        self.assertEqual(result.expected_new_count, 0)
        self.assertEqual(result.expected_existing_count, 1)
        self.assertEqual(result.blocked_count, 1)

    @staticmethod
    def plan(model, **versions):
        return CompiledMigrationPlan(
            plan_id="archive_records",
            origin="profile_document",
            origin_hash=HASH,
            datasets=(
                DatasetSpec(
                    name="records",
                    source=SourceSpec(file="records.csv"),
                    target=TargetSpec(model=model, mode="update"),
                    source_identity=SourceIdentitySpec(fields=("code",)),
                    target_identity=TargetIdentitySpec(
                        components=(
                            IdentityComponent(
                                source_fields=("code",), target_fields=("code",)
                            ),
                        )
                    ),
                    fields={
                        "active": FieldSpec(
                            source="active", type="boolean", compare=True
                        )
                    },
                ),
            ),
            **versions,
        )
