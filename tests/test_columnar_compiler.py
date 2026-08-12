from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest

import polars as pl

from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.compiler.columnar_transformation import (
    COLUMNAR_CAPABILITY_MATRIX,
    ColumnarCompilationError,
    ColumnarDatasetKind,
    ColumnarExecutionClass,
    ColumnarOperationKind,
    ColumnarScalarFieldProgram,
    ColumnarSupport,
    ColumnarTransformationProgram,
    compile_columnar_transformation_program,
    compile_columnar_transformation_programs,
)
from impodo.domain.mapping.contracts import (
    BusinessControlTotal,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    ReferenceLookupMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueError,
    ScalarValueRuleError,
    evaluate_scalar_mapping_value,
)
from impodo.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    TextTransformStep,
)
from impodo.workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
DATASET_ID = "dataset:0123456789abcdef01234567"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class ColumnarCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = _selection()

    def test_capability_matrix_is_complete_unique_and_explicit(self) -> None:
        by_operation = {item.operation: item for item in COLUMNAR_CAPABILITY_MATRIX}

        self.assertEqual(len(by_operation), len(COLUMNAR_CAPABILITY_MATRIX))
        self.assertEqual(set(by_operation), set(ColumnarOperationKind))
        for item in COLUMNAR_CAPABILITY_MATRIX:
            with self.subTest(operation=item.operation):
                if item.execution_class is ColumnarExecutionClass.PYTHON_ORACLE:
                    self.assertTrue(item.fallback_code)
                    self.assertTrue(item.fallback_message)
                else:
                    self.assertIsNone(item.fallback_code)
                    self.assertIsNone(item.fallback_message)

    def test_domain_compiler_does_not_import_polars(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "impodo"
            / "domain"
            / "compiler"
            / "columnar_transformation.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )

        self.assertFalse(any(name.startswith("polars") for name in imported))

    def test_supported_program_is_deterministic_and_projects_only_inputs(self) -> None:
        definition = _supported_definition(self.selection)

        first = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        second = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        reversed_fields = tuple(reversed(definition.datasets[0].fields))
        reordered = replace(
            definition,
            datasets=(replace(definition.datasets[0], fields=reversed_fields),),
        )
        third = compile_columnar_transformation_program(
            reordered,
            self.selection,
            DATASET_ID,
        )

        self.assertEqual(first.support, ColumnarSupport.SUPPORTED)
        self.assertEqual(first.fallback_reasons, ())
        self.assertIsNotNone(first.program)
        assert first.program is not None
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.content_hash, third.content_hash)
        self.assertEqual(first.program.content_hash, third.program.content_hash)
        self.assertEqual(
            tuple(item.stable_key for item in first.program.inputs),
            (
                "product.id",
                "product.name",
                "product.category",
                "product.quantity",
                "product.ordered_on",
                "product.updated_at",
            ),
        )
        self.assertEqual(
            tuple(item.target_field for item in first.program.scalar_fields),
            (
                "active",
                "category",
                "name",
                "ordered_on",
                "quantity",
                "updated_at",
            ),
        )
        self.assertTrue(first.program.preserve_source_row)
        self.assertTrue(first.program.preserve_source_order)
        self.assertTrue(first.program.sparse_transformation_impacts)
        self.assertEqual(first.program.target_model, "product.template")
        self.assertEqual(first.program.target_mode, "upsert")
        self.assertEqual(first.program.source_identity[0].source_label, "id")
        name_program = next(
            item for item in first.program.scalar_fields if item.target_field == "name"
        )
        self.assertEqual(name_program.source_label, "name")
        self.assertEqual(
            name_program.transformation_rules,
            "Source + Trim + Collapse spaces + Find and replace + "
            "Case: uppercase + Empty to null",
        )
        self.assertEqual(
            tuple(item.operation for item in first.program.set_requirements),
            (
                ColumnarOperationKind.DUPLICATE_IDENTITY_GROUPING,
                ColumnarOperationKind.CONTROL_TOTAL,
            ),
        )
        restored = ColumnarTransformationProgram.from_portable_dict(
            first.program.to_portable_dict()
        )
        self.assertEqual(restored, first.program)
        self.assertEqual(restored.content_hash, first.program.content_hash)

    def test_fallback_and_value_match_order_are_explicit(self) -> None:
        field = ScalarFieldMapping(
            target_field="name",
            source_column_key="product.name",
            value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
            literal_value=" missing ",
            value_mappings=(ValueMapping("N/A", "not available"),),
            transform=ScalarTransformPolicy(
                trim=True,
                collapse_whitespace=True,
                empty_as_null=True,
                case_mode="lowercase",
                text_steps=(
                    TextTransformStep(
                        search_value="-",
                        replacement_value=" ",
                        replace_all=False,
                    ),
                ),
            ),
            required=True,
        )
        decision = _compile_fields(self.selection, (field,))

        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        program = decision.program.scalar_fields[0]
        self.assertEqual(
            tuple(item.operation for item in program.provider.fallback_probe_steps),
            (
                ColumnarOperationKind.TRIM,
                ColumnarOperationKind.COLLAPSE_WHITESPACE,
                ColumnarOperationKind.CASE_LOWER,
                ColumnarOperationKind.EMPTY_AS_NULL,
            ),
        )
        self.assertEqual(program.provider.value_mappings, (("N/A", "not available"),))
        self.assertTrue(program.provider.value_mapping_bypasses_transforms)
        self.assertEqual(
            tuple(item.operation for item in program.transform_steps),
            (
                ColumnarOperationKind.RENDER_TEXT,
                ColumnarOperationKind.TRIM,
                ColumnarOperationKind.COLLAPSE_WHITESPACE,
                ColumnarOperationKind.REPLACE_LITERAL,
                ColumnarOperationKind.CASE_LOWER,
                ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH,
                ColumnarOperationKind.EMPTY_AS_NULL,
            ),
        )
        self.assertEqual(
            program.required_step.operation,
            ColumnarOperationKind.REQUIRE_VALUE,
        )
        self.assertEqual(
            program.conversion_step.operation,
            ColumnarOperationKind.PARSE_STRING,
        )

    def test_each_oracle_only_construct_has_a_stable_reason(self) -> None:
        base = ScalarFieldMapping(
            target_field="name",
            source_column_key="product.name",
        )
        reference = ReferenceLookupMapping(
            reference_id="12345678-1234-5678-1234-567812345678",
            reference_content_hash=HASH_A,
            key_source_column_keys=("product.name",),
            value_field="name",
        )
        cases = (
            (
                replace(
                    base,
                    transform=ScalarTransformPolicy(formula="upper(value)"),
                ),
                "COLUMNAR_FORMULA_UNSUPPORTED",
            ),
            (
                replace(
                    base,
                    transform=ScalarTransformPolicy(
                        text_steps=(
                            TextTransformStep(
                                search_value=r"\s+",
                                replacement_value=" ",
                                search_mode="pattern",
                            ),
                        ),
                    ),
                ),
                "COLUMNAR_PATTERN_REPLACEMENT_UNSUPPORTED",
            ),
            (
                replace(base, reference_lookup=reference),
                "COLUMNAR_REFERENCE_LOOKUP_UNSUPPORTED",
            ),
            (
                replace(
                    base,
                    value_type="decimal",
                    transform=ScalarTransformPolicy(decimal_places=2),
                ),
                "COLUMNAR_DECIMAL_ROUNDING_UNSUPPORTED",
            ),
            (
                replace(base, value_type="datetime"),
                "COLUMNAR_ISO_DATETIME_UNSUPPORTED",
            ),
            (
                replace(
                    base,
                    validation=ScalarValidationPolicy(pattern=r"[A-Z]+"),
                ),
                "COLUMNAR_CUSTOM_PATTERN_UNSUPPORTED",
            ),
            (
                replace(
                    base,
                    value_type="integer",
                    validation=ScalarValidationPolicy(exact_length=3),
                ),
                "COLUMNAR_TYPED_VALIDATION_UNSUPPORTED",
            ),
            (
                replace(
                    base,
                    transform=ScalarTransformPolicy(case_mode="title"),
                ),
                "COLUMNAR_TITLE_CASE_UNSUPPORTED",
            ),
        )
        for field, code in cases:
            with self.subTest(code=code):
                first = _compile_fields(self.selection, (field,))
                second = _compile_fields(self.selection, (field,))
                self.assertEqual(first.support, ColumnarSupport.PYTHON_FALLBACK)
                self.assertIsNone(first.program)
                self.assertIn(code, {item.code for item in first.fallback_reasons})
                self.assertEqual(first.content_hash, second.content_hash)

    def test_relationship_identity_resolver_and_non_direct_dataset_fallback(self) -> None:
        resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="res.category",
            key_mappings=(ReferenceKeyMapping("product.category", "code"),),
        )
        relationship = RelationshipMapping(
            target_field="category_id",
            kind="many2one",
            source_column_keys=("product.category",),
            resolver=resolver,
        )
        identity = IdentityComponentMapping(
            source_column_keys=("product.category",),
            target_fields=("category_id",),
            resolver=resolver,
        )
        definition = _definition(
            self.selection,
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                ),
            ),
            target_identity=(identity,),
            relationships=(relationship,),
        )
        direct = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        related = compile_columnar_transformation_program(
            _supported_definition(self.selection),
            self.selection,
            DATASET_ID,
            dataset_kind=ColumnarDatasetKind.RELATED_CHILD,
        )

        self.assertEqual(direct.support, ColumnarSupport.PYTHON_FALLBACK)
        self.assertEqual(
            {item.code for item in direct.fallback_reasons},
            {
                "COLUMNAR_IDENTITY_RESOLVER_UNSUPPORTED",
                "COLUMNAR_RELATIONSHIP_UNSUPPORTED",
            },
        )
        self.assertEqual(related.support, ColumnarSupport.PYTHON_FALLBACK)
        self.assertEqual(
            {item.code for item in related.fallback_reasons},
            {"COLUMNAR_NON_DIRECT_DATASET_UNSUPPORTED"},
        )

    def test_incoming_many2one_compiles_native_key_once_for_set_resolution(
        self,
    ) -> None:
        parent = self.selection.datasets[0]
        bom_id = "dataset:fedcba9876543210fedcba98"
        bom = SourceDataset(
            dataset_id=bom_id,
            name="bom",
            source=FileSourceBinding(
                file_id="file-bom",
                table_key="sheet:bom",
                source_sha256=HASH_C,
                catalog_hash=HASH_B,
                encoding=None,
                delimiter=None,
                header_row=1,
            ),
            row_count=500,
            columns=(
                SourceDatasetColumn(
                    ordinal=1,
                    source_name="line_id",
                    stable_key="bom.line_id",
                    candidate_type="string",
                ),
                SourceDatasetColumn(
                    ordinal=2,
                    source_name="product_id",
                    stable_key="bom.product_id",
                    candidate_type="string",
                ),
            ),
        )
        selection = replace(self.selection, datasets=(parent, bom))
        definition = MappingDefinition(
            mapping_id="mapping-product-bom",
            source_selection_hash=selection.content_hash,
            schema_hash=HASH_B,
            datasets=(
                DatasetMapping(
                    dataset_id=parent.dataset_id,
                    target_model="product.product",
                    source_identity_column_keys=("product.id",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("product.id",),
                            target_fields=("default_code",),
                        ),
                    ),
                ),
                DatasetMapping(
                    dataset_id=bom_id,
                    target_model="mrp.bom.line",
                    source_identity_column_keys=("bom.line_id",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("bom.line_id",),
                            target_fields=("x_import_key",),
                        ),
                    ),
                    relationships=(
                        RelationshipMapping(
                            target_field="product_id",
                            kind="many2one",
                            source_column_keys=("bom.product_id",),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id=parent.dataset_id,
                            ),
                            required=True,
                        ),
                    ),
                ),
            ),
        )

        decision = compile_columnar_transformation_program(
            definition,
            selection,
            bom_id,
        )

        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        self.assertEqual(len(decision.program.relationships), 1)
        relationship = decision.program.relationships[0]
        self.assertEqual(relationship.parent_dataset_name, "products")
        self.assertEqual(
            tuple(
                item.operation
                for item in relationship.key.normalization_steps
            ),
            (
                ColumnarOperationKind.TRIM,
                ColumnarOperationKind.EMPTY_AS_NULL,
                ColumnarOperationKind.PARSE_STRING,
            ),
        )
        operations = {
            item.operation for item in decision.capability_uses
        }
        self.assertIn(
            ColumnarOperationKind.RELATIONSHIP_KEY_NORMALIZATION,
            operations,
        )
        self.assertIn(
            ColumnarOperationKind.RELATIONSHIP_RESOLUTION,
            operations,
        )
        self.assertEqual(
            ColumnarTransformationProgram.from_portable_dict(
                decision.program.to_portable_dict()
            ),
            decision.program,
        )

    def test_one_unsupported_field_forces_whole_dataset_fallback(self) -> None:
        supported = ScalarFieldMapping(
            target_field="name",
            source_column_key="product.name",
            transform=ScalarTransformPolicy(trim=True),
        )
        unsupported = ScalarFieldMapping(
            target_field="quantity",
            source_column_key="product.quantity",
            value_type="decimal",
            transform=ScalarTransformPolicy(decimal_places=2),
        )

        decision = _compile_fields(self.selection, (supported, unsupported))

        self.assertEqual(decision.support, ColumnarSupport.PYTHON_FALLBACK)
        self.assertIsNone(decision.program)
        self.assertEqual(
            {item.target_field for item in decision.fallback_reasons},
            {"quantity"},
        )
        self.assertTrue(
            any(
                item.target_field == "name"
                and item.execution_class is ColumnarExecutionClass.NATIVE_COLUMNAR
                for item in decision.capability_uses
            )
        )

    def test_compiler_rejects_stale_or_incomplete_selection_bindings(self) -> None:
        definition = _supported_definition(self.selection)
        stale = replace(self.selection, content_hash=HASH_C)
        incomplete = replace(definition, datasets=())

        with self.assertRaisesRegex(ColumnarCompilationError, "no longer matches"):
            compile_columnar_transformation_programs(definition, stale)
        with self.assertRaisesRegex(ColumnarCompilationError, "cover every"):
            compile_columnar_transformation_programs(incomplete, self.selection)


class ColumnarOperationParityTests(unittest.TestCase):
    """Prototype native expressions against the existing semantic oracle."""

    def setUp(self) -> None:
        self.selection = _selection()

    def test_provider_and_text_operation_parity(self) -> None:
        cases = (
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                ),
                (None, "  Alpha  ", 42, True),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    value_source=ScalarValueSource.CONSTANT,
                    literal_value=" Fixed ",
                    transform=ScalarTransformPolicy(trim=True),
                ),
                (None, "ignored"),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
                    literal_value="FALLBACK",
                    transform=ScalarTransformPolicy(
                        trim=True,
                        collapse_whitespace=True,
                        empty_as_null=True,
                        case_mode="lowercase",
                    ),
                ),
                (None, "   ", "  A   B "),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    value_mappings=(
                        ValueMapping("A", "Mapped"),
                        ValueMapping("B", "Second"),
                    ),
                    transform=ScalarTransformPolicy(case_mode="lowercase"),
                ),
                (" A ", "B", "C"),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    transform=ScalarTransformPolicy(
                        trim=True,
                        collapse_whitespace=True,
                        text_steps=(
                            TextTransformStep(
                                search_value="-",
                                replacement_value=" ",
                            ),
                        ),
                        case_mode="uppercase",
                        empty_as_null=True,
                    ),
                ),
                ("  one-two   three ", "straße", ""),
            ),
        )
        for field, raw_values in cases:
            with self.subTest(field=field):
                _assert_polars_parity(self, self.selection, field, raw_values)

    def test_scalar_conversion_parity(self) -> None:
        cases = (
            (
                ScalarFieldMapping(
                    target_field="quantity",
                    source_column_key="product.quantity",
                    value_type="integer",
                ),
                (
                    None,
                    "0",
                    "+17",
                    "-4",
                    "1234567890123456789012345678901234567890",
                    "١٢٣",
                    "1.5",
                    "yes",
                ),
            ),
            (
                ScalarFieldMapping(
                    target_field="quantity",
                    source_column_key="product.quantity",
                    value_type="decimal",
                ),
                (
                    None,
                    "0",
                    "+17.2500",
                    "-4.00",
                    "1234567890123456789012345678901234567890.00100",
                    "1,234.50",
                    "1.5.0",
                ),
            ),
            (
                ScalarFieldMapping(
                    target_field="quantity",
                    source_column_key="product.quantity",
                    value_type="decimal",
                    transform=ScalarTransformPolicy(decimal_locale="fr_FR"),
                ),
                (None, "1 234,50", "1\u202f234,50", "1234,50", "1,234.50"),
            ),
            (
                ScalarFieldMapping(
                    target_field="active",
                    source_column_key="product.name",
                    value_type="boolean",
                ),
                (None, "true", "YES", "y", "false", "0", "maybe"),
            ),
            (
                ScalarFieldMapping(
                    target_field="ordered_on",
                    source_column_key="product.ordered_on",
                    value_type="date",
                    transform=ScalarTransformPolicy(date_format="iso"),
                ),
                (None, "2026-01-02", "2026-02-30", "02/01/2026"),
            ),
            (
                ScalarFieldMapping(
                    target_field="ordered_on",
                    source_column_key="product.ordered_on",
                    value_type="date",
                    transform=ScalarTransformPolicy(date_format="dmy_slash"),
                ),
                (None, "02/01/2026", "31/02/2026", "2026-01-02"),
            ),
            (
                ScalarFieldMapping(
                    target_field="updated_at",
                    source_column_key="product.updated_at",
                    value_type="datetime",
                    transform=ScalarTransformPolicy(date_format="dmy_slash"),
                ),
                (None, "02/01/2026 03:04:05", "31/02/2026 03:04:05"),
            ),
        )
        for field, raw_values in cases:
            with self.subTest(value_type=field.value_type, format=field.transform.date_format):
                _assert_polars_parity(self, self.selection, field, raw_values)

    def test_required_and_basic_validation_parity(self) -> None:
        cases = (
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    required=True,
                    transform=ScalarTransformPolicy(trim=True, empty_as_null=True),
                ),
                (None, "  ", "ok"),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    validation=ScalarValidationPolicy(exact_length=3),
                ),
                (None, "ABC", "AB", "ABCD", "ééé"),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    validation=ScalarValidationPolicy(
                        segment_location="first",
                        segment_length=2,
                        character_class="uppercase",
                    ),
                ),
                (None, "AB-12", "Ab-12", "A", "12-AB"),
            ),
            (
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                    validation=ScalarValidationPolicy(
                        segment_location="entire",
                        character_class="digits",
                    ),
                ),
                (None, "123", "12A", "", "１２３"),
            ),
        )
        for field, raw_values in cases:
            with self.subTest(validation=field.validation):
                _assert_polars_parity(self, self.selection, field, raw_values)


def _selection() -> SourceSelection:
    names = (
        ("id", "string"),
        ("name", "string"),
        ("category", "string"),
        ("quantity", "integer"),
        ("ordered_on", "date"),
        ("updated_at", "datetime"),
        ("unused", "string"),
    )
    dataset = SourceDataset(
        dataset_id=DATASET_ID,
        name="products",
        source=FileSourceBinding(
            file_id="file-products",
            table_key="sheet:products",
            source_sha256=HASH_A,
            catalog_hash=HASH_B,
            encoding=None,
            delimiter=None,
            header_row=1,
        ),
        row_count=100,
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
        selection_id="selection-columnar",
        version=1,
        project_id="project-columnar",
        created_at=NOW,
        created_by="tester",
        datasets=(dataset,),
        content_hash=HASH_A,
    )


def _supported_definition(selection: SourceSelection) -> MappingDefinition:
    fields = (
        ScalarFieldMapping(
            target_field="name",
            source_column_key="product.name",
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
                case_mode="uppercase",
            ),
        ),
        ScalarFieldMapping(
            target_field="active",
            value_source=ScalarValueSource.CONSTANT,
            literal_value="yes",
            value_type="boolean",
        ),
        ScalarFieldMapping(
            target_field="category",
            source_column_key="product.category",
            value_mappings=(
                ValueMapping("Retail", "retail"),
                ValueMapping("Wholesale", "wholesale"),
            ),
        ),
        ScalarFieldMapping(
            target_field="quantity",
            source_column_key="product.quantity",
            value_type="integer",
        ),
        ScalarFieldMapping(
            target_field="ordered_on",
            source_column_key="product.ordered_on",
            value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
            literal_value="2026-01-01",
            value_type="date",
            transform=ScalarTransformPolicy(trim=True, empty_as_null=True),
        ),
        ScalarFieldMapping(
            target_field="updated_at",
            source_column_key="product.updated_at",
            value_type="datetime",
            transform=ScalarTransformPolicy(date_format="dmy_slash"),
        ),
        ScalarFieldMapping(
            target_field="company_id",
            value_source=ScalarValueSource.ODOO_DEFAULT,
            compare=False,
        ),
    )
    return _definition(
        selection,
        fields=fields,
        control_totals=(
            BusinessControlTotal(
                name="Quantity",
                target_field="quantity",
                expected_total="100",
            ),
        ),
    )


def _definition(
    selection: SourceSelection,
    *,
    fields: tuple[ScalarFieldMapping, ...],
    target_identity: tuple[IdentityComponentMapping, ...] | None = None,
    relationships: tuple[RelationshipMapping, ...] = (),
    control_totals: tuple[BusinessControlTotal, ...] = (),
) -> MappingDefinition:
    return MappingDefinition(
        mapping_id="mapping-columnar",
        source_selection_hash=selection.content_hash,
        schema_hash=HASH_B,
        datasets=(
            DatasetMapping(
                dataset_id=DATASET_ID,
                target_model="product.template",
                source_identity_column_keys=("product.id",),
                target_identity=(
                    target_identity
                    if target_identity is not None
                    else (
                        IdentityComponentMapping(
                            source_column_keys=("product.id",),
                            target_fields=("default_code",),
                        ),
                    )
                ),
                fields=fields,
                relationships=relationships,
                control_totals=control_totals,
            ),
        ),
    )


def _compile_fields(
    selection: SourceSelection,
    fields: tuple[ScalarFieldMapping, ...],
):
    return compile_columnar_transformation_program(
        _definition(selection, fields=fields),
        selection,
        DATASET_ID,
    )


def _assert_polars_parity(
    testcase: unittest.TestCase,
    selection: SourceSelection,
    mapping: ScalarFieldMapping,
    raw_values: tuple[object, ...],
) -> None:
    decision = _compile_fields(selection, (mapping,))
    testcase.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
    assert decision.program is not None
    field = decision.program.scalar_fields[0]
    native = _evaluate_polars_prototype(field, raw_values)
    oracle = tuple(_oracle_result(mapping, raw) for raw in raw_values)
    testcase.assertEqual(native, oracle)


def _oracle_result(mapping: ScalarFieldMapping, raw: object) -> tuple[str, object]:
    try:
        return ("ok", evaluate_scalar_mapping_value(mapping, raw))
    except ScalarValueRuleError as error:
        return ("rule", error.code)
    except ScalarValueError:
        return ("scalar", None)


def _evaluate_polars_prototype(
    field: ColumnarScalarFieldProgram,
    raw_values: tuple[object, ...],
) -> tuple[tuple[str, object], ...]:
    """Small test-only native prototype; Slice 4 owns production execution."""

    frame = pl.DataFrame(
        {"raw": [None if item is None else str(item) for item in raw_values]},
        schema={"raw": pl.String},
    )
    raw = pl.col("raw")
    provider = field.provider
    if provider.operation is ColumnarOperationKind.READ_SOURCE:
        proposed = raw
    elif provider.operation is ColumnarOperationKind.USE_CONSTANT:
        # Bind a constant to the input height; a literal-only projection has
        # scalar height in Polars and is not itself a row program.
        proposed = (
            pl.when(raw.is_null() | raw.is_not_null())
            .then(pl.lit(provider.literal_value, dtype=pl.String))
            .otherwise(pl.lit(provider.literal_value, dtype=pl.String))
        )
    elif provider.operation is ColumnarOperationKind.SOURCE_FALLBACK:
        probe = _apply_text_steps(raw, provider.fallback_probe_steps)
        proposed = (
            pl.when(probe.is_null())
            .then(pl.lit(provider.literal_value, dtype=pl.String))
            .otherwise(probe)
        )
    else:
        raise AssertionError(provider.operation)

    transformed = _apply_text_steps(proposed, field.transform_steps)
    if provider.value_mappings:
        choice = raw.str.strip_chars()
        mapped = pl.lit(None, dtype=pl.String)
        matched = pl.lit(False)
        for source_value, target_value in provider.value_mappings:
            mapped = pl.when(choice == source_value).then(pl.lit(target_value)).otherwise(mapped)
            matched = matched | (choice == source_value)
        prepared = pl.when(matched).then(mapped).otherwise(transformed)
    else:
        prepared = transformed

    required_invalid = (
        prepared.is_null()
        if field.required_step is not None
        else pl.lit(False)
    )
    converted = _convert_expression(prepared, field.conversion_step)
    parse_invalid = prepared.is_not_null() & converted.is_null()
    output_too_long = pl.lit(False)
    for step in field.transform_steps:
        if step.operation is ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH:
            assert step.integer is not None
            output_too_long = prepared.str.len_chars() > step.integer

    exact_invalid = pl.lit(False)
    character_invalid = pl.lit(False)
    for step in field.validation_steps:
        if step.operation is ColumnarOperationKind.VALIDATE_EXACT_LENGTH:
            assert step.integer is not None
            exact_invalid = converted.is_not_null() & (
                converted.str.len_chars() != step.integer
            )
        elif step.operation is ColumnarOperationKind.VALIDATE_CHARACTER_CLASS:
            character_invalid = _character_invalid(converted, step)
        else:
            raise AssertionError(step.operation)

    result = frame.select(
        converted.alias("value"),
        output_too_long.alias("output_too_long"),
        required_invalid.alias("required_invalid"),
        parse_invalid.alias("parse_invalid"),
        exact_invalid.alias("exact_invalid"),
        character_invalid.alias("character_invalid"),
    ).rows(named=True)
    portable: list[tuple[str, object]] = []
    for row in result:
        if row["output_too_long"]:
            portable.append(("rule", "SOURCE_RULE_OUTPUT_TOO_LONG"))
        elif row["required_invalid"] or row["parse_invalid"]:
            portable.append(("scalar", None))
        elif row["exact_invalid"]:
            portable.append(("rule", "SOURCE_TEXT_LENGTH_INVALID"))
        elif row["character_invalid"]:
            portable.append(("rule", "SOURCE_TEXT_SEGMENT_INVALID"))
        else:
            value = row["value"]
            if (
                value is not None
                and field.conversion_step.operation
                is ColumnarOperationKind.PARSE_INTEGER
            ):
                # Native execution validates the unbounded integer grammar;
                # the bounded canonical adapter constructs Python's arbitrary-
                # precision integer without imposing an Int64 range.
                value = int(value, 10)
            elif (
                value is not None
                and field.conversion_step.operation
                is ColumnarOperationKind.PARSE_DECIMAL
            ):
                value = Decimal(value)
            portable.append(("ok", value))
    return tuple(portable)


def _apply_text_steps(
    expression: pl.Expr,
    steps,
) -> pl.Expr:
    result = expression
    for step in steps:
        operation = step.operation
        if operation is ColumnarOperationKind.RENDER_TEXT:
            continue
        if operation is ColumnarOperationKind.TRIM:
            result = result.str.strip_chars()
        elif operation is ColumnarOperationKind.COLLAPSE_WHITESPACE:
            result = result.str.replace_all(r"\s+", " ")
        elif operation is ColumnarOperationKind.REPLACE_LITERAL:
            assert step.text is not None and step.replacement is not None
            result = (
                result.str.replace_all(
                    step.text,
                    step.replacement,
                    literal=True,
                )
                if step.flag
                else result.str.replace(
                    step.text,
                    step.replacement,
                    literal=True,
                    n=1,
                )
            )
        elif operation is ColumnarOperationKind.CASE_UPPER:
            result = result.str.to_uppercase()
        elif operation is ColumnarOperationKind.CASE_LOWER:
            result = result.str.to_lowercase()
        elif operation is ColumnarOperationKind.EMPTY_AS_NULL:
            result = pl.when(result == "").then(pl.lit(None, dtype=pl.String)).otherwise(result)
        elif operation is ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH:
            continue
        else:
            raise AssertionError(operation)
    return result


def _convert_expression(expression: pl.Expr, step) -> pl.Expr:
    operation = step.operation
    if operation is ColumnarOperationKind.PARSE_STRING:
        return expression
    if operation is ColumnarOperationKind.PARSE_INTEGER:
        valid = expression.str.contains(r"^[+-]?\d+$")
        return pl.when(valid).then(expression).otherwise(None)
    if operation is ColumnarOperationKind.PARSE_DECIMAL:
        locale = step.text
        patterns = {
            "invariant": r"^[+-]?\d+(?:\.\d+)?$",
            "en_US": r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$",
            "de_DE": r"^[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$",
            "fr_FR": (
                r"^[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)"
                r"(?:,\d+)?$"
            ),
        }
        valid = expression.str.contains(patterns[locale])
        normalized = expression
        if locale == "en_US":
            normalized = normalized.str.replace_all(",", "", literal=True)
        elif locale == "de_DE":
            normalized = normalized.str.replace_all(".", "", literal=True)
            normalized = normalized.str.replace_all(",", ".", literal=True)
        elif locale == "fr_FR":
            normalized = normalized.str.replace_all(r"[ \u00a0\u202f]", "")
            normalized = normalized.str.replace_all(",", ".", literal=True)
        return pl.when(valid).then(normalized).otherwise(None)
    if operation is ColumnarOperationKind.PARSE_BOOLEAN:
        token = expression.str.to_lowercase()
        return (
            pl.when(token.is_in(["true", "1", "yes", "y"]))
            .then(pl.lit(True))
            .when(token.is_in(["false", "0", "no", "n"]))
            .then(pl.lit(False))
            .otherwise(None)
        )
    if operation is ColumnarOperationKind.PARSE_DATE:
        formats = {
            "iso": "%Y-%m-%d",
            "dmy_slash": "%d/%m/%Y",
            "mdy_slash": "%m/%d/%Y",
            "dmy_dot": "%d.%m.%Y",
        }
        return expression.str.strptime(
            pl.Date,
            formats[step.text],
            strict=False,
            exact=True,
        )
    if operation is ColumnarOperationKind.PARSE_DATETIME:
        if step.text == "iso":
            aware = expression.str.to_datetime(
                strict=False,
                exact=True,
                time_zone="UTC",
            )
            naive = pl.coalesce(
                expression.str.strptime(
                    pl.Datetime,
                    "%Y-%m-%d %H:%M:%S%.f",
                    strict=False,
                    exact=True,
                ),
                expression.str.strptime(
                    pl.Datetime,
                    "%Y-%m-%dT%H:%M:%S%.f",
                    strict=False,
                    exact=True,
                ),
            ).dt.replace_time_zone("UTC")
            return pl.coalesce(aware, naive)
        formats = {
            "dmy_slash": "%d/%m/%Y %H:%M:%S",
            "mdy_slash": "%m/%d/%Y %H:%M:%S",
            "dmy_dot": "%d.%m.%Y %H:%M:%S",
        }
        return expression.str.strptime(
            pl.Datetime,
            formats[step.text],
            strict=False,
            exact=True,
        ).dt.replace_time_zone("UTC")
    raise AssertionError(operation)


def _character_invalid(expression: pl.Expr, step) -> pl.Expr:
    assert step.character_class is not None
    assert step.segment_location is not None
    patterns = {
        "digits": r"^[0-9]+$",
        "uppercase": r"^[A-Z]+$",
        "lowercase": r"^[a-z]+$",
    }
    segment = expression
    too_short = pl.lit(False)
    if step.segment_location in {"first", "last"}:
        assert step.segment_length is not None
        too_short = expression.str.len_chars() < step.segment_length
        segment = expression.str.slice(
            0 if step.segment_location == "first" else -step.segment_length,
            step.segment_length,
        )
    mismatch = ~segment.str.contains(patterns[step.character_class])
    return expression.is_not_null() & (too_short | mismatch.fill_null(True))


if __name__ == "__main__":
    unittest.main()
