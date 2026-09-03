from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import polars as pl

from impodo.adapters.polars_transformation import (
    POLARS_TRANSFORMATION_BATCH_ROWS,
    iter_polars_prepared_batches,
    write_polars_prepared_snapshot,
)
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    ColumnarTransformationProgram,
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.contracts import (
    MAX_VALUE_MAPPINGS,
    ConcatenationBlankHandling,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.evaluator import compile_browser_row_transformer
from impodo.domain.staging.transformation_impact import (
    TransformationImpactCounts,
    _TransformationImpactCollector,
)
from impodo.domain.shared.models import PreparedRecord
from impodo.domain.preparation.source import CompiledPreparedRowTransformer, SourceRow
from impodo.application.data_version.source_snapshots import SourceSnapshotCandidateWriter
from impodo.domain.recipe.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    TextTransformStep,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
DATASET_ID = "dataset:0123456789abcdef01234567"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class PolarsTransformationParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.selection = _selection()
        self.definition = _definition(self.selection)
        self.rows = _rows()
        self.path, self.snapshot = _write_snapshot(
            self.root,
            self.selection,
            self.rows,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepared_batches_match_python_oracle_for_all_chunk_sizes(self) -> None:
        expected_records, expected_report = _python_oracle(
            self.definition,
            self.selection,
            self.rows,
        )
        self.assertEqual(len(expected_report.rule_impacts), 1)
        self.assertEqual(
            (
                expected_report.rule_impacts[0].evaluated_value_count,
                expected_report.rule_impacts[0].matched_value_count,
                expected_report.rule_impacts[0].changed_value_count,
            ),
            (4, 3, 3),
        )
        decision = compile_columnar_transformation_program(
            self.definition,
            self.selection,
            DATASET_ID,
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        destination, prepared = _write_prepared_snapshot(
            self.root,
            self.path,
            self.snapshot,
            decision.program,
        )

        for chunk_size in (1, 17, POLARS_TRANSFORMATION_BATCH_ROWS):
            with self.subTest(chunk_size=chunk_size):
                records = []
                collector = _TransformationImpactCollector(
                    mapping_content_hash=self.definition.content_hash,
                    detail_limit=10_000,
                )
                observed_batch_sizes = []
                for batch in iter_polars_prepared_batches(
                    destination,
                    prepared,
                    self.snapshot,
                    decision.program,
                    batch_size=chunk_size,
                ):
                    observed_batch_sizes.append(len(batch.records))
                    records.extend(batch.records)
                    collector.record_precomputed(
                        batch.impact_counts,
                        batch.impacts,
                        batch.rule_impacts,
                    )

                self.assertEqual(tuple(records), expected_records)
                self.assertEqual(collector.report(), expected_report)
                self.assertTrue(observed_batch_sizes)
                self.assertLessEqual(max(observed_batch_sizes), chunk_size)

    def test_maximum_value_mapping_cardinality_matches_python_oracle(self) -> None:
        dataset = self.definition.datasets[0]
        category = next(
            field for field in dataset.fields if field.target_field == "category"
        )
        value_mappings = (
            ValueMapping("Retail", "retail"),
            ValueMapping("Wholesale", "wholesale"),
            *(
                ValueMapping(f"Choice {index}", f"choice_{index}")
                for index in range(MAX_VALUE_MAPPINGS - 2)
            ),
        )
        definition = canonicalize_mapping_definition(
            replace(
                self.definition,
                datasets=(
                    replace(
                        dataset,
                        fields=tuple(
                            replace(field, value_mappings=value_mappings)
                            if field is category
                            else field
                            for field in dataset.fields
                        ),
                    ),
                ),
            )
        )
        expected_records, expected_report = _python_oracle(
            definition,
            self.selection,
            self.rows,
        )
        decision = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        destination, prepared = _write_prepared_snapshot(
            self.root,
            self.path,
            self.snapshot,
            decision.program,
        )

        records = tuple(
            record
            for batch in iter_polars_prepared_batches(
                destination,
                prepared,
                self.snapshot,
                decision.program,
                batch_size=2,
            )
            for record in batch.records
        )

        self.assertEqual(records, expected_records)
        collector = _TransformationImpactCollector(
            mapping_content_hash=definition.content_hash,
            detail_limit=10_000,
        )
        for batch in iter_polars_prepared_batches(
            destination,
            prepared,
            self.snapshot,
            decision.program,
            batch_size=2,
        ):
            collector.record_precomputed(
                batch.impact_counts,
                batch.impacts,
                batch.rule_impacts,
            )
        self.assertEqual(collector.report(), expected_report)

    def test_combined_source_columns_match_python_oracle_for_both_blank_policies(
        self,
    ) -> None:
        for blank_handling in ConcatenationBlankHandling:
            with self.subTest(blank_handling=blank_handling.value):
                combined = ScalarFieldMapping(
                    target_field="display_name",
                    value_source=ScalarValueSource.CONCATENATE,
                    concatenation=ScalarConcatenation(
                        source_column_keys=(
                            "product.name",
                            "product.category",
                        ),
                        separator=" / ",
                        blank_handling=blank_handling,
                        trim_parts=True,
                    ),
                )
                definition = canonicalize_mapping_definition(
                    MappingDefinition(
                        mapping_id="mapping-concatenation",
                        source_selection_hash=self.selection.content_hash,
                        schema_hash=HASH_B,
                        datasets=(
                            DatasetMapping(
                                dataset_id=DATASET_ID,
                                target_model="product.template",
                                source_identity_column_keys=("product.id",),
                                target_identity=(
                                    IdentityComponentMapping(
                                        source_column_keys=("product.sku",),
                                        target_fields=("default_code",),
                                    ),
                                ),
                                fields=(combined,),
                            ),
                        ),
                    )
                )
                expected_records, expected_report = _python_oracle(
                    definition,
                    self.selection,
                    self.rows,
                )
                decision = compile_columnar_transformation_program(
                    definition,
                    self.selection,
                    DATASET_ID,
                )
                self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
                assert decision.program is not None
                policy_root = self.root / blank_handling.value
                policy_root.mkdir()
                destination, prepared = _write_prepared_snapshot(
                    policy_root,
                    self.path,
                    self.snapshot,
                    decision.program,
                )
                collector = _TransformationImpactCollector(
                    mapping_content_hash=definition.content_hash,
                    detail_limit=10_000,
                )
                records = []
                for batch in iter_polars_prepared_batches(
                    destination,
                    prepared,
                    self.snapshot,
                    decision.program,
                    batch_size=2,
                ):
                    records.extend(batch.records)
                    collector.record_precomputed(
                        batch.impact_counts,
                        batch.impacts,
                        batch.rule_impacts,
                    )

                self.assertEqual(tuple(records), expected_records)
                self.assertEqual(collector.report(), expected_report)

    def test_incoming_relationship_keys_match_python_oracle_across_batches(
        self,
    ) -> None:
        dataset_mapping = replace(
            self.definition.datasets[0],
            relationships=(
                RelationshipMapping(
                    target_field="parent_id",
                    kind="many2one",
                    source_column_keys=("product.category",),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.DATASET,
                        dataset_id=DATASET_ID,
                    ),
                ),
            ),
        )
        definition = replace(
            self.definition,
            datasets=(dataset_mapping,),
        )
        expected_records, _expected_report = _python_oracle(
            definition,
            self.selection,
            self.rows,
        )
        decision = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        destination, prepared = _write_prepared_snapshot(
            self.root,
            self.path,
            self.snapshot,
            decision.program,
        )

        for chunk_size in (1, 3, POLARS_TRANSFORMATION_BATCH_ROWS):
            with self.subTest(chunk_size=chunk_size):
                records = tuple(
                    record
                    for batch in iter_polars_prepared_batches(
                        destination,
                        prepared,
                        self.snapshot,
                        decision.program,
                        batch_size=chunk_size,
                    )
                    for record in batch.records
                )
                self.assertEqual(records, expected_records)

    def test_production_projection_skips_full_prepared_record_objects(self) -> None:
        expected_records, _expected_report = _python_oracle(
            self.definition,
            self.selection,
            self.rows,
        )
        decision = compile_columnar_transformation_program(
            self.definition,
            self.selection,
            DATASET_ID,
        )
        assert decision.program is not None
        destination, prepared = _write_prepared_snapshot(
            self.root,
            self.path,
            self.snapshot,
            decision.program,
        )

        batches = []
        with patch.object(
            PreparedRecord,
            "from_canonicalized_values",
            side_effect=AssertionError("production path built PreparedRecord"),
        ):
            batches.extend(
                iter_polars_prepared_batches(
                    destination,
                    prepared,
                    self.snapshot,
                    decision.program,
                    batch_size=2,
                    materialize_records=False,
                )
            )

        self.assertTrue(batches)
        self.assertTrue(all(not batch.records for batch in batches))
        self.assertEqual(
            tuple(
                identity for batch in batches for identity in batch.source_identities
            ),
            tuple(record.source_identity for record in expected_records),
        )
        self.assertEqual(
            tuple(values for batch in batches for values in batch.scalar_values),
            tuple(record.scalar_values for record in expected_records),
        )

    def test_native_ordered_cleanup_matches_python_and_counts_each_step(self) -> None:
        dataset = self.definition.datasets[0]
        name_field = next(
            field for field in dataset.fields if field.target_field == "name"
        )
        ordered_name = replace(
            name_field,
            transform=ScalarTransformPolicy(
                trim=True,
                collapse_whitespace=True,
                empty_as_null=True,
                text_steps=(
                    TextTransformStep(
                        search_value="Alpha",
                        replacement_value="A",
                        search_mode="starts_with",
                    ),
                    TextTransformStep(
                        search_value="-",
                        replacement_value=" ",
                    ),
                ),
            ),
        )
        definition = canonicalize_mapping_definition(
            replace(
                self.definition,
                datasets=(
                    replace(
                        dataset,
                        fields=tuple(
                            ordered_name if field.target_field == "name" else field
                            for field in dataset.fields
                        ),
                    ),
                ),
            )
        )
        expected_records, expected_report = _python_oracle(
            definition,
            self.selection,
            self.rows,
        )
        decision = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        destination, prepared = _write_prepared_snapshot(
            self.root,
            self.path,
            self.snapshot,
            decision.program,
        )
        collector = _TransformationImpactCollector(
            mapping_content_hash=definition.content_hash,
            detail_limit=10_000,
        )
        records = []
        for batch in iter_polars_prepared_batches(
            destination,
            prepared,
            self.snapshot,
            decision.program,
            batch_size=2,
        ):
            records.extend(batch.records)
            collector.record_precomputed(
                batch.impact_counts,
                batch.impacts,
                batch.rule_impacts,
            )

        self.assertEqual(tuple(records), expected_records)
        self.assertEqual(collector.report(), expected_report)
        counts_by_kind = {
            item.rule_kind: (
                item.evaluated_value_count,
                item.matched_value_count,
                item.changed_value_count,
            )
            for item in expected_report.rule_impacts
        }
        self.assertEqual(
            counts_by_kind,
            {
                "find_replace_starts_with": (4, 1, 1),
                "find_replace_literal": (4, 3, 3),
            },
        )

    def test_snapshot_row_count_and_program_binding_fail_closed(self) -> None:
        decision = compile_columnar_transformation_program(
            self.definition,
            self.selection,
            DATASET_ID,
        )
        assert decision.program is not None
        short_path = self.root / "short.parquet"
        pl.read_parquet(self.path).head(3).write_parquet(short_path)
        other_program = replace(decision.program, dataset_name="other")

        with self.assertRaisesRegex(ValueError, "row accounting"):
            write_polars_prepared_snapshot(
                short_path,
                self.snapshot,
                decision.program,
                self.root / "short-prepared.parquet",
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            write_polars_prepared_snapshot(
                self.path,
                self.snapshot,
                other_program,
                self.root / "other-prepared.parquet",
            )

    def test_prepared_snapshot_corruption_and_binding_fail_closed(self) -> None:
        decision = compile_columnar_transformation_program(
            self.definition,
            self.selection,
            DATASET_ID,
        )
        assert decision.program is not None
        destination, prepared = _write_prepared_snapshot(
            self.root,
            self.path,
            self.snapshot,
            decision.program,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            tuple(
                iter_polars_prepared_batches(
                    destination,
                    prepared,
                    self.snapshot,
                    replace(decision.program, dataset_name="other"),
                )
            )

        destination.write_bytes(destination.read_bytes()[:16])
        with self.assertRaisesRegex(ValueError, "schema is unreadable"):
            tuple(
                iter_polars_prepared_batches(
                    destination,
                    prepared,
                    self.snapshot,
                    decision.program,
                )
            )

    def test_precomputed_impact_counts_reject_incomplete_sparse_rows(self) -> None:
        collector = _TransformationImpactCollector(
            mapping_content_hash=self.definition.content_hash,
            detail_limit=0,
        )
        counts = TransformationImpactCounts(
            evaluated_count=2,
            changed_count=1,
            unchanged_count=1,
        )

        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            collector.record_precomputed(counts, ())

    def test_native_adapter_has_no_python_udf_or_complete_collect_escape_hatch(
        self,
    ) -> None:
        source = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "adapters"
            / "polars_transformation.py"
        ).read_text(encoding="utf-8")

        self.assertIn("scan_parquet(", source)
        self.assertIn("collect_batches(", source)
        for forbidden in (
            ".map_elements(",
            ".map_batches(",
            ".collect(",
            ".to_dicts(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


def _write_prepared_snapshot(
    root: Path,
    source_path: Path,
    snapshot: SourceSnapshot,
    program: ColumnarTransformationProgram,
) -> tuple[Path, PreparedSnapshot]:
    destination = root / "prepared.parquet"
    candidate = write_polars_prepared_snapshot(
        source_path,
        snapshot,
        program,
        destination,
    )
    return destination, PreparedSnapshot.create(
        workspace_id="11111111-1111-4111-8111-111111111111",
        dataset_id=snapshot.dataset_id,
        dataset_name=snapshot.dataset_name,
        source_snapshot_hash=snapshot.content_hash,
        mapping_hash=program.mapping_content_hash,
        schema_hash=program.schema_hash,
        transformation_program_hash=program.content_hash,
        row_count=candidate.row_count,
        physical_schema_hash=candidate.physical_schema_hash,
        parquet_sha256=candidate.parquet_sha256,
        created_at=NOW,
    )


def _selection() -> SourceSelection:
    names = (
        ("id", "string"),
        ("sku", "string"),
        ("scope", "string"),
        ("name", "string"),
        ("quantity", "integer"),
        ("price", "decimal"),
        ("active", "boolean"),
        ("ordered_on", "date"),
        ("updated_at", "datetime"),
        ("category", "string"),
        ("fixed_code", "string"),
        ("optional_text", "string"),
        ("required_text", "string"),
        ("validated_required", "string"),
    )
    dataset = SourceDataset(
        dataset_id=DATASET_ID,
        name="products",
        source=FileSourceBinding(
            file_id="file-products",
            table_key="csv",
            source_sha256=HASH_A,
            catalog_hash=HASH_B,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=4,
        columns=tuple(
            SourceDatasetColumn(
                ordinal=index,
                source_name=name,
                stable_key=f"product.{name}",
                candidate_type=candidate,
            )
            for index, (name, candidate) in enumerate(names, start=1)
        ),
    )
    return SourceSelection(
        selection_id="selection-polars",
        version=1,
        data_version_id="project-polars",
        created_at=NOW,
        created_by="tester",
        datasets=(dataset,),
        content_hash=HASH_A,
    )


def _definition(selection: SourceSelection) -> MappingDefinition:
    definition = MappingDefinition(
        mapping_id="mapping-polars",
        source_selection_hash=selection.content_hash,
        schema_hash=HASH_B,
        datasets=(
            DatasetMapping(
                dataset_id=DATASET_ID,
                target_model="product.template",
                source_identity_column_keys=("product.id",),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=("product.sku",),
                        target_fields=("default_code",),
                    ),
                ),
                target_scope=(
                    IdentityComponentMapping(
                        source_column_keys=("product.scope",),
                        target_fields=("company_code",),
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        target_field="name",
                        source_column_key="product.name",
                        value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
                        literal_value="missing-name",
                        transform=ScalarTransformPolicy(
                            trim=True,
                            collapse_whitespace=True,
                            empty_as_null=True,
                            text_steps=(
                                TextTransformStep(
                                    search_value="-",
                                    replacement_value=" ",
                                ),
                            ),
                            case_mode="lowercase",
                        ),
                        required=True,
                    ),
                    ScalarFieldMapping(
                        target_field="quantity",
                        source_column_key="product.quantity",
                        value_type="integer",
                        required=True,
                    ),
                    ScalarFieldMapping(
                        target_field="list_price",
                        source_column_key="product.price",
                        value_type="decimal",
                        transform=ScalarTransformPolicy(decimal_locale="fr_FR"),
                    ),
                    ScalarFieldMapping(
                        target_field="active",
                        source_column_key="product.active",
                        value_type="boolean",
                    ),
                    ScalarFieldMapping(
                        target_field="ordered_on",
                        source_column_key="product.ordered_on",
                        value_type="date",
                        transform=ScalarTransformPolicy(date_format="dmy_slash"),
                    ),
                    ScalarFieldMapping(
                        target_field="updated_at",
                        source_column_key="product.updated_at",
                        value_type="datetime",
                        transform=ScalarTransformPolicy(date_format="dmy_slash"),
                    ),
                    ScalarFieldMapping(
                        target_field="category",
                        source_column_key="product.category",
                        value_mappings=(
                            ValueMapping("Retail", "retail"),
                            ValueMapping("Wholesale", "wholesale"),
                        ),
                        transform=ScalarTransformPolicy(case_mode="uppercase"),
                    ),
                    ScalarFieldMapping(
                        target_field="fixed_code",
                        source_column_key="product.fixed_code",
                        validation=ScalarValidationPolicy(
                            exact_length=3,
                            segment_location="entire",
                            character_class="uppercase",
                        ),
                    ),
                    ScalarFieldMapping(
                        target_field="origin",
                        value_source=ScalarValueSource.CONSTANT,
                        literal_value="LOCAL",
                    ),
                    ScalarFieldMapping(
                        target_field="optional_text",
                        source_column_key="product.optional_text",
                    ),
                    ScalarFieldMapping(
                        target_field="required_text",
                        source_column_key="product.required_text",
                        required=True,
                    ),
                    ScalarFieldMapping(
                        target_field="validated_required",
                        source_column_key="product.validated_required",
                        required=True,
                        validation=ScalarValidationPolicy(exact_length=3),
                    ),
                ),
            ),
        ),
    )
    return canonicalize_mapping_definition(definition)


def _rows() -> tuple[SourceRow, ...]:
    values = (
        {
            "id": " P-1 ",
            "sku": " SKU   1 ",
            "scope": " BE ",
            "name": "  Alpha-One  ",
            "quantity": 1,
            "price": "1 234,500",
            "active": True,
            "ordered_on": "09/08/2026",
            "updated_at": "09/08/2026 14:30:15",
            "category": " Retail ",
            "fixed_code": "ABC",
            "optional_text": "",
            "required_text": "",
            "validated_required": "",
        },
        {
            "id": "P-2",
            "sku": "SKU-2",
            "scope": "BE",
            "name": "Beta",
            "quantity": "1.5",
            "price": Decimal("10.2500"),
            "active": "no",
            "ordered_on": "31/02/2026",
            "updated_at": "invalid",
            "category": "Other",
            "fixed_code": "Ab1",
            "optional_text": None,
            "required_text": "ok",
            "validated_required": "ABC",
        },
        {
            "id": "P-3",
            "sku": "SKU-3",
            "scope": "BE",
            "name": "   ",
            "quantity": "+0007",
            "price": "1\u202f234,50",
            "active": "YES",
            "ordered_on": None,
            "updated_at": None,
            "category": "Wholesale",
            "fixed_code": "XY",
            "optional_text": "",
            "required_text": "x",
            "validated_required": "XYZ",
        },
        {
            "id": None,
            "sku": "  SKU-4  ",
            "scope": "BE",
            "name": None,
            "quantity": "1234567890123456789012345678901234567890",
            "price": "1234567890123456789012345678901234567890,00100",
            "active": "maybe",
            "ordered_on": "10/08/2026",
            "updated_at": "10/08/2026 00:00:00",
            "category": "Retail",
            "fixed_code": "１２３",
            "optional_text": " ",
            "required_text": "y",
            "validated_required": "QQQ",
        },
    )
    return tuple(
        SourceRow(number=index, values=item)
        for index, item in enumerate(values, start=2)
    )


def _python_oracle(
    definition: MappingDefinition,
    selection: SourceSelection,
    rows: tuple[SourceRow, ...],
):
    dataset = selection.datasets[0]
    mapping = definition.datasets[0]
    transformer = compile_browser_row_transformer(
        dataset,
        dataset,
        mapping,
        None,
        "source",
    )
    compiled = compile_browser_mapping(definition, selection).datasets[0]
    preparer = CompiledPreparedRowTransformer.compile(compiled, transformer.headers)
    collector = _TransformationImpactCollector(
        mapping_content_hash=definition.content_hash,
        detail_limit=10_000,
    )
    records = []
    for row in rows:
        staged, issues = transformer.finish(
            transformer.project(row),
            impact_collector=collector,
        )
        record = preparer.transform(staged)
        if issues:
            record = replace(record, issues=(*record.issues, *issues))
        records.append(record)
    return tuple(records), collector.report()


def _write_snapshot(
    root: Path,
    selection: SourceSelection,
    rows: tuple[SourceRow, ...],
) -> tuple[Path, SourceSnapshot]:
    dataset = selection.datasets[0]
    schema = SourceSnapshotSchema.create(
        SourceSnapshotColumn.create(
            ordinal=item.ordinal,
            stable_key=item.stable_key,
            source_name=item.source_name,
            candidate_type=item.candidate_type,
        )
        for item in dataset.columns
    )
    writer = SourceSnapshotCandidateWriter(
        root,
        schema,
        batch_rows=max(1, len(rows)),
    )
    writer.append_source_rows(rows)
    candidate = writer.finalize()
    snapshot = SourceSnapshot.create(
        data_version_id=selection.data_version_id,
        dataset_id=dataset.dataset_id,
        dataset_name=dataset.name,
        source=dataset.source,
        physical_selection_hash=selection.content_hash,
        schema=schema,
        row_count=len(rows),
        data_logical_hash=candidate.data_logical_hash,
        parquet_sha256=candidate.parquet_sha256,
        created_at=selection.created_at,
    )
    return candidate.path, snapshot


if __name__ == "__main__":
    unittest.main()
