from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePath
from types import SimpleNamespace
from uuid import uuid4

from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.contracts import CompiledMigrationPlan
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
    ValueMapping,
)
from impodo.domain.odoo.contracts import (
    MetadataSnapshot,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import (
    CompiledPreparedRowTransformer,
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    BusinessReference,
    Classification,
    FieldMetadata,
    LogicalReference,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    portable_value,
    restore_portable_value,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.staging.evaluator import compile_browser_row_transformer
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

_HASH = "sha256:" + "1" * 64


def _plan() -> CompiledMigrationPlan:
    sales_uoms = DatasetSpec(
        name="sales_uoms",
        source=SourceSpec(file="sales_uoms.csv"),
        target=TargetSpec(model="uom.uom", mode="upsert"),
        source_identity=SourceIdentitySpec(fields=("uom_code",)),
        target_identity=TargetIdentitySpec(
            components=(
                IdentityComponent(
                    source_fields=("uom_code",),
                    target_fields=("name",),
                ),
            ),
        ),
        fields={
            "rounding": FieldSpec(
                source="rounding",
                type="decimal",
                required=True,
                required_on_create=True,
            ),
        },
    )
    products = DatasetSpec(
        name="products",
        source=SourceSpec(file="products.csv"),
        target=TargetSpec(model="product.template", mode="upsert"),
        source_identity=SourceIdentitySpec(fields=("product_code",)),
        target_identity=TargetIdentitySpec(
            components=(
                IdentityComponent(
                    source_fields=("product_code",),
                    target_fields=("default_code",),
                ),
            ),
        ),
        fields={
            "name": FieldSpec(
                source="product_name",
                type="string",
                required=True,
                required_on_create=True,
            ),
        },
        relations={
            "uom_id": RelationSpec(
                kind="many2one",
                source_fields=("uom_code",),
                resolve=ResolveSpec(
                    dataset="sales_uoms",
                    target_source_fields=("uom_code",),
                    target_model="uom.uom",
                    target_fields=("name",),
                    target_value_mappings=(("UNI", "Unit"),),
                ),
                required=True,
                required_on_create=True,
            ),
        },
    )
    return CompiledMigrationPlan(
        plan_id="target_first_uoms",
        origin="profile_document",
        origin_hash=_HASH,
        datasets=(sales_uoms, products),
    )


def _tables(*, uom_codes: tuple[str, ...]) -> tuple[SourceTable, ...]:
    uom_rows = tuple(
        SourceRow(
            number=index + 2,
            values={
                "uom_code": code,
                "rounding": {
                    "PCE": "1",
                    "UNI": "0.5",
                    "kg": "0.01",
                    "m": "0.1",
                    "KG": "0.01",
                }[code],
            },
        )
        for index, code in enumerate(uom_codes)
    )
    product_rows = tuple(
        SourceRow(
            number=index + 2,
            values={
                "product_code": f"P-{code}",
                "product_name": f"Product in {code}",
                "uom_code": code,
            },
        )
        for index, code in enumerate(uom_codes)
    )
    return (
        SourceTable(
            dataset="sales_uoms",
            path=PurePath("sales_uoms.csv"),
            headers=("uom_code", "rounding"),
            rows=uom_rows,
            content_hash=_HASH,
        ),
        SourceTable(
            dataset="products",
            path=PurePath("products.csv"),
            headers=("product_code", "product_name", "uom_code"),
            rows=product_rows,
            content_hash=_HASH,
        ),
    )


def _snapshots() -> tuple[MetadataSnapshot, RecordSnapshot]:
    fingerprint = TargetFingerprint(
        target_hash=_HASH,
        connection_mode="LOCAL",
        database="target_first_test",
        odoo_version="19.0",
        snapshot_timestamp="2026-08-27T00:00:00Z",
    )
    metadata = MetadataSnapshot(
        fingerprint=fingerprint,
        models={
            "uom.uom": ModelMetadata(
                model="uom.uom",
                description="Units of Measure",
                fields={
                    "name": FieldMetadata(
                        name="name",
                        type="char",
                        required=True,
                    ),
                    "rounding": FieldMetadata(
                        name="rounding",
                        type="float",
                        required=True,
                    ),
                },
            ),
            "product.template": ModelMetadata(
                model="product.template",
                description="Products",
                fields={
                    "default_code": FieldMetadata(
                        name="default_code",
                        type="char",
                    ),
                    "name": FieldMetadata(
                        name="name",
                        type="char",
                        required=True,
                    ),
                    "uom_id": FieldMetadata(
                        name="uom_id",
                        type="many2one",
                        required=True,
                        relation="uom.uom",
                    ),
                },
            ),
        },
    )
    records = RecordSnapshot(
        fingerprint=fingerprint,
        records={
            "uom.uom": (
                TargetRecord(
                    "uom.uom",
                    10,
                    {"name": "Unit", "rounding": Decimal(1)},
                ),
                TargetRecord(
                    "uom.uom",
                    11,
                    {"name": "kg", "rounding": Decimal("0.001")},
                ),
                TargetRecord(
                    "uom.uom",
                    12,
                    {"name": "m", "rounding": Decimal("0.01")},
                ),
            ),
            "product.template": (),
        },
        requested_fields={
            "uom.uom": ("name", "rounding"),
            "product.template": ("default_code", "name", "uom_id"),
        },
    )
    return bind_snapshot_hashes(metadata, records)


def _prepared(plan: CompiledMigrationPlan, *codes: str):
    return prepare_source_tables(
        plan,
        _tables(uom_codes=tuple(codes)),
        source_hashes={
            "sales_uoms": _HASH,
            "products": _HASH,
        },
    )


def _frozen(plan: CompiledMigrationPlan, prepared):
    return SimpleNamespace(
        workspace_id=str(uuid4()),
        prepared=prepared,
        plan=plan,
        revision=SimpleNamespace(
            mapping_id=str(uuid4()),
            version=1,
            definition=SimpleNamespace(content_hash=_HASH),
        ),
        staging=SimpleNamespace(run_id=str(uuid4()), content_hash=_HASH),
        quality=SimpleNamespace(run_id=str(uuid4()), content_hash=_HASH),
        normalization=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=_HASH,
            lifecycle_version=1,
            eligible_dataset_hash=_HASH,
        ),
        content_hash=_HASH,
    )


class TargetFirstRelationshipTests(unittest.TestCase):
    def test_browser_mapping_compiles_both_reference_origins(self) -> None:
        sales_dataset_id = "dataset:" + "a" * 24
        product_dataset_id = "dataset:" + "b" * 24

        def source(name: str) -> FileSourceBinding:
            return FileSourceBinding(
                file_id=f"source:{name}",
                table_key=name,
                source_sha256=_HASH,
                catalog_hash=_HASH,
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            )

        selection = SourceSelection(
            selection_id="target-first-selection",
            version=1,
            data_version_id="target-first-data",
            created_at=datetime(2026, 8, 27, tzinfo=UTC),
            created_by="tester",
            datasets=(
                SourceDataset(
                    dataset_id=sales_dataset_id,
                    name="sales_uoms",
                    source=source("sales_uoms"),
                    row_count=4,
                    columns=(
                        SourceDatasetColumn(1, "UoM", "uom.code", "string"),
                    ),
                ),
                SourceDataset(
                    dataset_id=product_dataset_id,
                    name="products",
                    source=source("products"),
                    row_count=4,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "Product code",
                            "product.code",
                            "string",
                        ),
                        SourceDatasetColumn(
                            2,
                            "Sales UoM",
                            "product.uom",
                            "string",
                        ),
                    ),
                ),
            ),
            content_hash=_HASH,
        )
        definition = MappingDefinition(
            mapping_id="target_first_mapping",
            source_selection_hash=selection.content_hash,
            schema_hash=_HASH,
            datasets=(
                DatasetMapping(
                    dataset_id=sales_dataset_id,
                    target_model="uom.uom",
                    source_identity_column_keys=("uom.code",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("uom.code",),
                            target_fields=("name",),
                        ),
                    ),
                ),
                DatasetMapping(
                    dataset_id=product_dataset_id,
                    target_model="product.template",
                    source_identity_column_keys=("product.code",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("product.code",),
                            target_fields=("default_code",),
                        ),
                    ),
                    relationships=(
                        RelationshipMapping(
                            target_field="uom_id",
                            kind="many2one",
                            source_column_keys=("product.uom",),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.TARGET_THEN_DATASET,
                                dataset_id=sales_dataset_id,
                                model="uom.uom",
                                key_mappings=(
                                    ReferenceKeyMapping("product.uom", "name"),
                                ),
                                value_mappings=(ValueMapping("UNI", "Unit"),),
                            ),
                        ),
                    ),
                ),
            ),
        )

        restored = MappingDefinition.from_json(definition.to_json())
        compiled = compile_browser_mapping(restored, selection)
        resolver = compiled.dataset("products").relations["uom_id"].resolve

        self.assertEqual(
            restored.datasets[1].relationships[0].resolver.origin,
            ResolverOrigin.TARGET_THEN_DATASET,
        )
        self.assertEqual(resolver.origin, "target_then_incoming")
        self.assertEqual(resolver.dataset, "sales_uoms")
        self.assertEqual(resolver.target_model, "uom.uom")
        self.assertEqual(resolver.target_fields, ("name",))
        self.assertEqual(resolver.target_value_mappings, (("UNI", "Unit"),))

        source_dataset = selection.datasets[1]
        transformer = compile_browser_row_transformer(
            source_dataset,
            source_dataset,
            restored.datasets[1],
            None,
            "source",
        )
        staged_row, issues = transformer.finish(
            transformer.project(
                SourceRow(
                    number=2,
                    values={"Product code": "P-UNI", "Sales UoM": "UNI"},
                )
            )
        )
        prepared = CompiledPreparedRowTransformer.compile(
            compiled.dataset("products"),
            transformer.headers,
        ).transform(staged_row)
        self.assertEqual(issues, ())
        self.assertEqual(
            prepared.references["uom_id"],
            LogicalReference(
                origin="target_then_incoming",
                key=("Unit",),
                dataset="sales_uoms",
                model="uom.uom",
                target_fields=("name",),
                incoming_key=("UNI",),
            ),
        )

    def test_odoo_wins_without_updates_and_missing_pce_uses_incoming(self) -> None:
        plan = _plan()
        prepared = _prepared(plan, "PCE", "UNI", "kg", "m")
        metadata, records = _snapshots()

        uni_product = next(
            row
            for row in prepared.records
            if row.dataset == "products" and row.source_identity == ("P-UNI",)
        )
        uni_reference = uni_product.references["uom_id"]
        self.assertEqual(
            uni_reference,
            LogicalReference(
                origin="target_then_incoming",
                key=("Unit",),
                dataset="sales_uoms",
                model="uom.uom",
                target_fields=("name",),
                incoming_key=("UNI",),
            ),
        )
        self.assertEqual(
            restore_portable_value(portable_value(uni_reference)),
            uni_reference,
        )

        result = PreflightEngine().run(plan, prepared, metadata, records)
        uom_decisions = {
            decision.business_identity[0]: decision
            for decision in result.decisions
            if decision.dataset == "sales_uoms"
        }
        self.assertEqual(uom_decisions["PCE"].classification, Classification.CREATE)
        for existing in ("Unit", "kg", "m"):
            with self.subTest(existing=existing):
                self.assertEqual(
                    uom_decisions[existing].classification,
                    Classification.UNCHANGED,
                )
                self.assertEqual(uom_decisions[existing].differences, ())
                self.assertIn(
                    "REFERENCE_TARGET_PRECEDENCE_REUSE",
                    {issue.code for issue in uom_decisions[existing].issues},
                )

        product_decisions = [
            decision
            for decision in result.decisions
            if decision.dataset == "products"
        ]
        self.assertTrue(
            all(
                decision.classification is Classification.CREATE
                for decision in product_decisions
            )
        )
        resolutions = {
            resolution.reference.incoming_key: resolution.status
            for resolution in result.reference_resolutions
            if resolution.dataset == "products"
        }
        self.assertEqual(resolutions[("PCE",)], "RESOLVED_INCOMING")
        self.assertEqual(resolutions[("UNI",)], "RESOLVED_TARGET")
        self.assertEqual(resolutions[("kg",)], "RESOLVED_TARGET")
        self.assertEqual(resolutions[("m",)], "RESOLVED_TARGET")

        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=_frozen(plan, prepared),
            result=result,
        )
        pce_product = next(
            row
            for row in snapshot.rows
            if row.dataset == "products" and row.source_identity == ("P-PCE",)
        )
        pce_uom = next(item for item in pce_product.fields if item.field == "uom_id")
        self.assertEqual(
            pce_uom.value,
            LogicalReference(
                origin="incoming",
                key=("PCE",),
                dataset="sales_uoms",
            ),
        )
        uni_product_row = next(
            row
            for row in snapshot.rows
            if row.dataset == "products" and row.source_identity == ("P-UNI",)
        )
        uni_uom = next(
            item for item in uni_product_row.fields if item.field == "uom_id"
        )
        self.assertEqual(
            uni_uom.value,
            BusinessReference("uom.uom", ("Unit",)),
        )
        reused_uoms = [
            row
            for row in snapshot.rows
            if row.dataset == "sales_uoms" and row.disposition == "UNCHANGED"
        ]
        self.assertEqual(len(reused_uoms), 3)
        self.assertTrue(all(not row.fields for row in reused_uoms))

    def test_case_only_odoo_candidate_requires_explicit_review(self) -> None:
        plan = _plan()
        prepared = _prepared(plan, "KG")
        metadata, records = _snapshots()

        result = PreflightEngine().run(plan, prepared, metadata, records)

        self.assertEqual(
            {decision.classification for decision in result.decisions},
            {Classification.BLOCKED},
        )
        self.assertIn(
            "REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED",
            {issue.code for decision in result.decisions for issue in decision.issues},
        )
        resolution = next(
            item
            for item in result.reference_resolutions
            if item.dataset == "products"
        )
        self.assertEqual(resolution.status, "CASE_MISMATCH")
        self.assertEqual(resolution.match_count, 1)

    def test_case_review_reads_are_batched_not_per_product(self) -> None:
        plan = _plan()
        prepared = _prepared(plan, "PCE", "UNI", "kg", "m")

        requests = plan_record_requests(plan, prepared.records)
        uom_requests = [request for request in requests if request.model == "uom.uom"]

        self.assertLessEqual(len(uom_requests), 3)
        self.assertTrue(
            any("=ilike" in str(request.domain) for request in uom_requests)
        )

    def test_ten_thousand_constant_references_plan_one_target_lookup(self) -> None:
        products = DatasetSpec(
            name="products",
            source=SourceSpec(file="products.csv"),
            target=TargetSpec(model="product.template", mode="upsert"),
            source_identity=SourceIdentitySpec(fields=("code",)),
            target_identity=TargetIdentitySpec(
                components=(
                    IdentityComponent(
                        source_fields=("code",),
                        target_fields=("default_code",),
                    ),
                ),
            ),
            relations={
                "uom_id": RelationSpec(
                    kind="many2one",
                    source_fields=(),
                    resolve=ResolveSpec(
                        target_model="uom.uom",
                        target_fields=("name",),
                    ),
                    value_source="constant_existing",
                    constant_key_values=("PCE",),
                    required=True,
                    required_on_create=True,
                ),
            },
        )
        plan = CompiledMigrationPlan(
            plan_id="constant_uom_scale",
            origin="profile_document",
            origin_hash=_HASH,
            datasets=(products,),
        )
        prepared = prepare_source_tables(
            plan,
            (
                SourceTable(
                    dataset="products",
                    path=PurePath("products.csv"),
                    headers=("code",),
                    rows=tuple(
                        SourceRow(index + 2, {"code": f"P-{index:05d}"})
                        for index in range(10_000)
                    ),
                    content_hash=_HASH,
                ),
            ),
            source_hashes={"products": _HASH},
        )

        requests = plan_record_requests(plan, prepared.records)
        uom_requests = [item for item in requests if item.model == "uom.uom"]

        self.assertEqual(len(uom_requests), 1)
        self.assertIn("PCE", str(uom_requests[0].domain))

if __name__ == "__main__":
    unittest.main()
