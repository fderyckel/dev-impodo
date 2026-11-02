"""Prepare a singleton company list from a portable constant, without a column."""

import unittest
from dataclasses import replace
from pathlib import PurePath
from types import SimpleNamespace

from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.execution.planner import plan_record_requests
from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
)
from impodo.domain.odoo.contracts import (
    MetadataSnapshot,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import (
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.shared.models import (
    Classification,
    FieldMetadata,
    LogicalReference,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from tests.domain.recipe.test_representative_shapes import _column, _dataset

HASH = "sha256:" + "1" * 64


class ConstantListRelationshipTests(unittest.TestCase):
    def inputs(self, count=2):
        dataset = _dataset("accounts", (("AccountNum", "string"),), "a")
        code = _column(dataset, "AccountNum")
        company = RelationshipMapping(
            "company_ids",
            "many2many",
            (),
            RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.company"),
            value_source=RelationshipValueSource.CONSTANT_EXISTING,
            constant_reference=ConstantBusinessReference(
                key_values=(ConstantReferenceComponent("name", "Example"),)
            ),
            required=True,
        )
        mapping = DatasetMapping(
            dataset.dataset_id,
            "account.account",
            source_identity_column_keys=(code,),
            target_identity=(IdentityComponentMapping((code,), ("code",)),),
            relationships=(company,),
        )
        definition = MappingDefinition("mapping:accounts", HASH, HASH, (mapping,))
        plan = compile_browser_mapping(
            definition, SimpleNamespace(datasets=(dataset,), content_hash=HASH)
        )
        table = SourceTable(
            dataset.name,
            PurePath("accounts.csv"),
            (code,),
            tuple(SourceRow(i + 2, {code: f"A{i}"}) for i in range(count)),
            HASH,
        )
        prepared = prepare_source_tables(
            plan, (table,), source_hashes={dataset.name: HASH}
        )
        return definition, plan, prepared

    def test_new_contract_round_trips_and_does_not_reinterpret_historical_versions(
        self,
    ):
        definition, _plan, _prepared = self.inputs()
        self.assertEqual(definition.contract_version, 21)
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        for version in (15, 19, 20):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(ValueError, "constant many2many"),
            ):
                replace(definition, contract_version=version)
        single = replace(definition.datasets[0].relationships[0], kind="many2one")
        old = replace(
            definition,
            contract_version=20,
            datasets=(replace(definition.datasets[0], relationships=(single,)),),
        )
        old_json, old_hash = old.to_json(), old.content_hash
        restored = MappingDefinition.from_json(old_json)
        self.assertEqual(restored.to_json(), old_json)
        self.assertEqual(restored.content_hash, old_hash)
        for operation in ("add", "remove"):
            with self.assertRaisesRegex(ValueError, "replace semantics"):
                replace(single, kind="many2many", operation=operation)

    def test_preparation_builds_one_company_reference_and_batches_shared_keys(self):
        _definition, plan, prepared = self.inputs(10_000)
        reference = LogicalReference(
            origin="target",
            key=("Example",),
            model="res.company",
            target_fields=("name",),
        )
        self.assertEqual(len(prepared.records), 10_000)
        self.assertTrue(
            all(
                row.references["company_ids"] == (reference,)
                for row in prepared.records
            )
        )
        companies = [
            r
            for r in plan_record_requests(plan, prepared.records)
            if r.model == "res.company"
        ]
        self.assertEqual(len(companies), 1)
        self.assertIn("Example", str(companies[0].domain))

    def test_both_majors_resolve_unique_company_and_block_missing_or_ambiguous_names(
        self,
    ):
        _definition, plan, prepared = self.inputs()
        for major in (19, 20):
            fingerprint = TargetFingerprint(
                HASH, "REMOTE", "fictional", f"{major}.0", "2026-11-02T00:00:00Z"
            )
            for count in (0, 1, 2):
                with self.subTest(major=major, company_count=count):
                    metadata, records = bind_snapshot_hashes(
                        MetadataSnapshot(
                            fingerprint,
                            {
                                "account.account": ModelMetadata(
                                    "account.account",
                                    "Account",
                                    {
                                        "code": FieldMetadata("code", "char"),
                                        "company_ids": FieldMetadata(
                                            "company_ids",
                                            "many2many",
                                            required=True,
                                            relation="res.company",
                                        ),
                                    },
                                ),
                                "res.company": ModelMetadata(
                                    "res.company",
                                    "Company",
                                    {
                                        "name": FieldMetadata(
                                            "name", "char", required=True
                                        )
                                    },
                                ),
                            },
                        ),
                        RecordSnapshot(
                            fingerprint,
                            {
                                "account.account": (),
                                "res.company": tuple(
                                    TargetRecord(
                                        "res.company", i + 1, {"name": "Example"}
                                    )
                                    for i in range(count)
                                ),
                            },
                            {
                                "account.account": ("code", "company_ids"),
                                "res.company": ("name",),
                            },
                        ),
                    )
                    result = PreflightEngine().run(plan, prepared, metadata, records)
                    expected = (
                        Classification.CREATE if count == 1 else Classification.BLOCKED
                    )
                    self.assertEqual(
                        {d.classification for d in result.decisions}, {expected}
                    )
                    self.assertEqual(len(result.decisions), 2)
                    self.assertEqual(
                        {d.dataset for d in result.decisions}, {"accounts"}
                    )
