"""Prepared reference projections stay native and preserve physical source data."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import polars as pl

from impodo.adapters.polars_reference import prepared_reference_expressions
from impodo.adapters.polars_transformation import iter_polars_prepared_batches
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    ColumnarTransformationProgram,
    compile_columnar_transformation_program,
)
from impodo.domain.compiler.reference_expression import compile_reference_preparation
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
    prepare_reference_value,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.source_snapshot import source_value_column
from impodo.domain.workspace.contracts import SourceDatasetColumn
from tests.integration.columnar import test_polars_transformation as fixtures


class PreparedReferenceNativeTests(TestCase):
    def fixture(self, preparation, *, identity=False):
        source = fixtures._selection().datasets[0]
        source = replace(
            source,
            columns=tuple(
                SourceDatasetColumn(index, name, name, "string")
                for index, name in enumerate(
                    ("id", "site", "warehouse", "bin", "company"), 1
                )
            ),
        )
        selection = replace(fixtures._selection(), datasets=(source,))
        key = ReferenceKeyMapping("site", "complete_name", preparation)
        scope = ReferenceKeyMapping("company", "company_id")
        resolver = RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="stock.location",
            key_mappings=(key,),
            scope_mappings=(scope,),
        )
        target_identity = (IdentityComponentMapping(("id",), ("name",)),)
        mapping = DatasetMapping(
            source.dataset_id,
            "stock.quant" if identity else "product.template",
            source_identity_column_keys=("id",),
            target_identity=target_identity,
            target_scope=(
                IdentityComponentMapping(
                    ("site", "company"), ("location_id",), resolver=resolver
                ),
            )
            if identity
            else (),
            relationships=()
            if identity
            else (
                RelationshipMapping(
                    "location_id", "many2one", ("site", "company"), resolver
                ),
            ),
        )
        definition = MappingDefinition(
            "prepared-reference", selection.content_hash, fixtures.HASH_B, (mapping,)
        )
        decision = compile_columnar_transformation_program(
            definition, selection, source.dataset_id
        )
        return selection, definition, decision, key

    def preparation(self):
        return ReferencePreparation(
            formula='concat(value, "/Stock/", column_3, "/", column_4)',
            bindings=(("column_3", "warehouse"), ("column_4", "bin")),
        )

    def test_formula_and_combined_parts_keep_leading_zeroes_and_bound_blank_inputs(
        self,
    ):
        preparations = (
            self.preparation(),
            ReferencePreparation(
                parts=(
                    ReferencePart(source_column_key="site"),
                    ReferencePart(literal="/Stock/"),
                    ReferencePart(source_column_key="warehouse"),
                    ReferencePart(literal="/"),
                    ReferencePart(source_column_key="bin"),
                )
            ),
        )
        frame = pl.DataFrame(
            {
                "site": ["0WIL", "0WIL", None],
                "warehouse": ["0100", None, None],
                "bin": ["100", "100", None],
            }
        )
        for preparation in preparations:
            with self.subTest(preparation=preparation):
                mapping = ReferenceKeyMapping("site", "complete_name", preparation)
                compiled = compile_reference_preparation(mapping)
                assert compiled is not None
                value, error = prepared_reference_expressions(compiled, pl.col)
                projected = frame.select(value.alias("value"), error.alias("error"))
                self.assertEqual(
                    projected["value"].to_list(), ["0WIL/Stock/0100/100", None, None]
                )
                self.assertEqual(
                    projected["error"].to_list(),
                    [None, "A source value needed for this reference is blank", None],
                )
                self.assertEqual(
                    prepare_reference_value(mapping, frame.row(0, named=True)),
                    projected["value"][0],
                )

    def test_scoped_relationship_and_relational_identity_run_native_and_roundtrip(self):
        for identity in (False, True):
            with self.subTest(identity=identity), TemporaryDirectory() as directory:
                selection, _definition, decision, _key = self.fixture(
                    self.preparation(), identity=identity
                )
                self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
                program = decision.program
                assert program is not None
                self.assertEqual(
                    {item.stable_key for item in program.inputs},
                    {"id", "site", "warehouse", "bin", "company"},
                )
                program = ColumnarTransformationProgram.from_portable_dict(
                    program.to_portable_dict()
                )
                self.assertEqual(program, decision.program)
                rows = (
                    SourceRow(
                        2,
                        {
                            "id": "1",
                            "site": "0WIL",
                            "warehouse": "0100",
                            "bin": "100",
                            "company": "Example",
                        },
                    ),
                    SourceRow(
                        3,
                        {
                            "id": "2",
                            "site": "0WIL",
                            "warehouse": None,
                            "bin": "100",
                            "company": "Example",
                        },
                    ),
                )
                root = Path(directory)
                path, snapshot = fixtures._write_snapshot(root, selection, rows)
                prepared_path, prepared = fixtures._write_prepared_snapshot(
                    root, path, snapshot, program
                )
                records = tuple(
                    record
                    for batch in iter_polars_prepared_batches(
                        prepared_path, prepared, snapshot, program, batch_size=1
                    )
                    for record in batch.records
                )
                reference = (
                    records[0].target_scope[0]
                    if identity
                    else records[0].references["location_id"]
                )
                self.assertEqual(reference.key, ("0WIL/Stock/0100/100",))
                self.assertEqual(reference.scope, ("Example",))
                self.assertEqual(
                    [item.code for item in records[1].issues],
                    ["REFERENCE_PREPARATION_INVALID"],
                )
                self.assertEqual(
                    pl.read_parquet(prepared_path)[source_value_column(2)].to_list(),
                    ["0WIL", "0WIL"],
                )
                self.assertEqual(
                    pl.read_parquet(prepared_path)[source_value_column(3)].to_list(),
                    ["0100", None],
                )

    def test_unproven_advanced_formula_reports_explicit_fallback(self):
        preparation = ReferencePreparation(formula='value + "/suffix"')
        _selection, _definition, decision, _key = self.fixture(preparation)
        self.assertEqual(decision.support, ColumnarSupport.PYTHON_FALLBACK)
        self.assertIn(
            "COLUMNAR_REFERENCE_PREPARATION_UNSUPPORTED",
            {item.code for item in decision.fallback_reasons},
        )

    def test_native_text_helpers_are_not_specific_to_locations(self):
        mapping = ReferenceKeyMapping(
            "account",
            "code",
            ReferencePreparation(
                formula='concat(upper(strip(value)), "-", column_2)',
                bindings=(("column_2", "suffix"),),
            ),
        )
        preparation = compile_reference_preparation(mapping)
        assert preparation is not None
        frame = pl.DataFrame({"account": [" ab "], "suffix": ["007"]})
        value, error = prepared_reference_expressions(preparation, pl.col)
        self.assertEqual(
            frame.select(value.alias("value"), error.alias("error")).row(0),
            ("AB-007", None),
        )
