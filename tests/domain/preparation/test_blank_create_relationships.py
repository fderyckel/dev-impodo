"""Exercise blank relationship decisions through preparation and execution intents."""

from dataclasses import replace
from pathlib import PurePath
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.execution.planner import plan_record_requests
from impodo.domain.execution_snapshot import build_execution_snapshot
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
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
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from tests.domain.recipe.test_representative_shapes import _column, _dataset

HASH = "sha256:" + "1" * 64


class BlankCreateRelationshipTests(TestCase):
    def test_new_records_use_selected_fallback_and_updates_keep_existing_value(self):
        for major in (19, 20):
            for fallback in ((), ("PCE",)):
                with self.subTest(major=major, fallback=fallback):
                    frozen, metadata, records = self._inputs(major, fallback)
                    requests = plan_record_requests(
                        frozen.plan, frozen.prepared.records
                    )
                    self.assertEqual("PCE" in str(requests), bool(fallback))
                    result = PreflightEngine().run(
                        frozen.plan, frozen.prepared, metadata, records
                    )
                    decisions = {item.source_row: item for item in result.decisions}
                    self.assertEqual(decisions[2].classification, Classification.CREATE)
                    self.assertEqual(
                        decisions[3].classification, Classification.UNCHANGED
                    )
                    self.assertEqual(decisions[3].differences, ())
                    self.assertEqual(decisions[4].classification, Classification.CREATE)
                    self.assertEqual(
                        decisions[5].classification, Classification.BLOCKED
                    )
                    execution = build_execution_snapshot(
                        preflight_run_id=str(uuid4()),
                        frozen=frozen,
                        result=result,
                    )
                    rows = {
                        row.source_row: {item.field: item for item in row.fields}
                        for row in execution.rows
                    }
                    self.assertEqual(rows[4]["uom_id"].value.key, ("kg",))
                    self.assertNotIn("uom_id", rows[3])
                    if fallback:
                        self.assertEqual(rows[2]["uom_id"].value.key, fallback)
                        self.assertTrue(rows[2]["uom_id"].target_binding_hashes)
                    else:
                        self.assertNotIn("uom_id", rows[2])

    def test_unverified_default_or_missing_existing_record_blocks_only_blank_creates(
        self,
    ):
        for fallback, code in (
            ((), "BLANK_CREATE_DEFAULT_UNVERIFIED"),
            (("PCE",), "BLANK_CREATE_RECORD_UNRESOLVED"),
        ):
            with self.subTest(fallback=fallback):
                frozen, metadata, records = self._inputs(20, fallback)
                metadata = replace(metadata, create_defaults={}, content_hash=None)
                records = replace(
                    records,
                    content_hash=None,
                    records={
                        **records.records,
                        "uom.uom": records.records["uom.uom"][:1],
                    },
                )
                metadata, records = bind_snapshot_hashes(metadata, records)
                result = PreflightEngine().run(
                    frozen.plan, frozen.prepared, metadata, records
                )
                decisions = {item.source_row: item for item in result.decisions}
                self.assertEqual(decisions[2].classification, Classification.BLOCKED)
                self.assertEqual([item.code for item in decisions[2].issues], [code])
                self.assertEqual(decisions[3].classification, Classification.UNCHANGED)
                self.assertEqual(decisions[4].classification, Classification.CREATE)

    def test_recipe_round_trip_keeps_blank_decision_with_current_source_binding(self):
        compiler = RecipeCompiler(None, None, None, None, None, None)
        for fallback in ((), ("PCE",)):
            with self.subTest(fallback=fallback):
                frozen, _, _ = self._inputs(20, fallback)
                dataset = frozen.revision.definition.datasets[0]
                code = dataset.source_identity_column_keys[0]
                unit = dataset.relationships[0].source_column_keys[0]
                portable = compiler._mapping(
                    (dataset,),
                    {dataset.dataset_id: "dataset:product"},
                    {
                        (dataset.dataset_id, code): "column:product.code",
                        (dataset.dataset_id, unit): "column:product.unit",
                    },
                    {},
                )
                relation = portable["datasets"][0]["relationships"][0]
                rebound = RecipeApplicationCompiler()._relationship(
                    relation,
                    {"column:product.unit": "new:unit"},
                )
                self.assertEqual(rebound.blank_create_values, fallback)
                self.assertEqual(rebound.source_column_keys, ("new:unit",))

    def _inputs(self, major, fallback):
        dataset = _dataset("product", (("Code", "string"), ("Unit", "string")), "a")
        code, unit = (_column(dataset, name) for name in ("Code", "Unit"))
        relation = RelationshipMapping(
            "uom_id",
            "many2one",
            (unit,),
            RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model="uom.uom",
                key_mappings=(ReferenceKeyMapping(unit, "name"),),
            ),
            required_on_create=True,
            blank_create_values=fallback,
        )
        mapping = DatasetMapping(
            dataset.dataset_id,
            "product.template",
            source_identity_column_keys=(code,),
            target_identity=(IdentityComponentMapping((code,), ("default_code",)),),
            relationships=(relation,),
        )
        definition = MappingDefinition("mapping:products", HASH, HASH, (mapping,))
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        with self.assertRaisesRegex(ValueError, "require mapping contract v23"):
            replace(definition, contract_version=22)
        plan = compile_browser_mapping(
            definition, SimpleNamespace(datasets=(dataset,), content_hash=HASH)
        )
        staged_unit = plan.dataset(dataset.name).relations["uom_id"].source_fields[0]
        table = SourceTable(
            dataset.name,
            PurePath("product.csv"),
            (code, staged_unit),
            tuple(
                SourceRow(number, {code: product, staged_unit: value})
                for number, product, value in (
                    (2, "Blank", " "),
                    (3, "Existing", None),
                    (4, "Weighted", "kg"),
                    (5, "Unmatched", "unknown"),
                )
            ),
            HASH,
        )
        prepared = prepare_source_tables(
            plan, (table,), source_hashes={dataset.name: HASH}
        )
        target = TargetFingerprint(
            HASH, "REMOTE", "fictional", f"{major}.0", "2026-11-04T00:00:00Z"
        )
        metadata, records = bind_snapshot_hashes(
            MetadataSnapshot(
                target,
                {
                    "product.template": ModelMetadata(
                        "product.template",
                        "Product",
                        {
                            "default_code": FieldMetadata("default_code", "char"),
                            "uom_id": FieldMetadata(
                                "uom_id", "many2one", required=True, relation="uom.uom"
                            ),
                        },
                    ),
                    "uom.uom": ModelMetadata(
                        "uom.uom", "Unit", {"name": FieldMetadata("name", "char")}
                    ),
                },
                create_defaults={"product.template": {"uom_id": 2}},
            ),
            RecordSnapshot(
                target,
                {
                    "product.template": (
                        TargetRecord(
                            "product.template",
                            11,
                            {"default_code": "Existing", "uom_id": (1, "kg")},
                        ),
                    ),
                    "uom.uom": (
                        TargetRecord("uom.uom", 1, {"name": "kg"}),
                        TargetRecord("uom.uom", 2, {"name": "PCE"}),
                    ),
                },
                {"product.template": ("default_code", "uom_id"), "uom.uom": ("name",)},
            ),
        )
        frozen = SimpleNamespace(
            workspace_id=str(uuid4()),
            prepared=prepared,
            plan=plan,
            revision=SimpleNamespace(
                mapping_id=definition.mapping_id, version=1, definition=definition
            ),
            staging=SimpleNamespace(run_id=str(uuid4()), content_hash=HASH),
            quality=SimpleNamespace(run_id=str(uuid4()), content_hash=HASH),
            normalization=SimpleNamespace(
                run_id=str(uuid4()),
                content_hash=HASH,
                lifecycle_version=2,
                eligible_dataset_hash=HASH,
            ),
            content_hash=HASH,
        )
        return frozen, metadata, records
