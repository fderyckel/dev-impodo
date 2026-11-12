"""Qualify native source identities and explicit ordering against the oracle."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from impodo.adapters.duckdb.native_prepared_projection import projected_encoded_rows_sql, supports_clean_native_projection
from impodo.adapters.polars_transformation import iter_polars_prepared_batches
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.columnar_transformation import ColumnarSupport, ColumnarTransformationProgram, compile_columnar_transformation_program
from impodo.domain.mapping.contracts import ScalarFieldMapping
from impodo.domain.mapping.generated_ordering import GENERATED_ORDER_SOURCE, GeneratedOrdering
from impodo.domain.mapping.source_record_identity import SourceRecordIdentity
from impodo.domain.preparation.source import CompiledPreparedRowTransformer, SourceRow
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.staging.evaluator import compile_browser_row_transformer
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from tests.integration.columnar import test_polars_transformation as fixtures


class SourceIdentityOrderingTests(unittest.TestCase):
    def exercise(self, values, *, ordering=None, existing=None, expect_clean=True):
        rows = tuple(SourceRow(number, {
            **{column.source_name: None for column in fixtures._selection().datasets[0].columns},
            **value,
        }) for number, value in enumerate(values, 2))
        selection = fixtures._selection()
        selection = replace(selection, datasets=(replace(selection.datasets[0], row_count=len(rows)),))
        definition = fixtures._definition(selection)
        mapping = replace(definition.datasets[0],
            target_model="x.document.line", source_identity_column_keys=("product.id",),
            target_identity=(), target_scope=(),
            source_record_identity=SourceRecordIdentity("legacy_lines", True, existing),
            generated_ordering=ordering,
            fields=(ScalarFieldMapping(target_field="sequence", source_column_key="product.quantity", value_type="integer", transform=ScalarTransformPolicy(empty_as_null=True)),),
        )
        definition = replace(definition, datasets=(mapping,))
        decision = compile_columnar_transformation_program(definition, selection, fixtures.DATASET_ID)
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED, decision.fallback_reasons)
        program = decision.program
        assert program is not None
        self.assertEqual(ColumnarTransformationProgram.from_portable_dict(program.to_portable_dict()), program)
        transformer = compile_browser_row_transformer(selection.datasets[0], selection.datasets[0], mapping, None, "source")
        preparer = CompiledPreparedRowTransformer.compile(compile_browser_mapping(definition, selection).datasets[0], transformer.headers)
        ordered = transformer.ordering_values(ordering, rows).values if ordering else None
        expected = []
        for row in rows:
            staged, issues = transformer.finish(transformer.project(row))
            if ordered is not None:
                staged = replace(staged, values={**staged.values, GENERATED_ORDER_SOURCE: ordered[row.number]})
            record = preparer.transform(staged)
            expected.append(replace(record, issues=(*record.issues, *issues)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, snapshot = fixtures._write_snapshot(root, selection, rows)
            prepared_path, prepared = fixtures._write_prepared_snapshot(root, path, snapshot, program)
            actual = tuple(record for batch in iter_polars_prepared_batches(prepared_path, prepared, snapshot, program, batch_size=2) for record in batch.records)
            self.assertEqual([row.source_identity for row in actual], [row.source_identity for row in expected])
            self.assertEqual([row.target_identity for row in actual], [row.target_identity for row in expected])
            self.assertEqual([row.scalar_values for row in actual], [row.scalar_values for row in expected])
            self.assertEqual([row.blocked for row in actual], [row.blocked for row in expected])
            projection = PreparedCanonicalProjection(
                dataset_id=fixtures.DATASET_ID, dataset="products", ordinal_start=0,
                row_count=len(rows), mode="upsert", source_hash=snapshot.content_hash,
                physical_dataset_id=fixtures.DATASET_ID, field_sources={}, program=program,
                set_based_projection=True,
            )
            with duckdb.connect(config={"threads": "1", "memory_limit": "192MB"}) as connection:
                self.assertEqual(supports_clean_native_projection(connection, prepared_path, program), expect_clean, connection.execute('SELECT "__impodo_columnar_issues" FROM read_parquet(?)', [str(prepared_path)]).fetchall())
                if expect_clean:
                    encoded = connection.execute(projected_encoded_rows_sql(projection), [str(prepared_path), 0, len(rows)]).fetchall()
                    self.assertEqual([json.loads(row[-1])["target_identity"] for row in encoded], [list(row.target_identity) for row in expected])
                    self.assertEqual([json.loads(row[-1])["proposed_values"] for row in encoded], [row.scalar_values for row in expected])
        return actual

    def test_source_identity_preserves_repeated_and_blank_values(self):
        actual = self.exercise((
            {"id": "É/\"\\\nline", "quantity": "10"},
            {"id": "second", "quantity": "10"},
            {"id": "third", "quantity": ""},
        ))
        self.assertEqual([row.scalar_values["sequence"] for row in actual], [10, 10, None])

    def test_existing_external_ids_are_retained_in_both_projectors(self):
        self.exercise(({"id": "a", "quantity": "10", "fixed_code": "export.old_line"}, {"id": "b", "quantity": "10", "fixed_code": ""}), existing="product.fixed_code")

    def test_file_order_can_be_generated_within_each_parent_group(self):
        actual = self.exercise((
            {"id": "a", "scope": "A", "quantity": "10"},
            {"id": "b", "scope": "B", "quantity": "10"},
            {"id": "c", "scope": "A", "quantity": ""},
        ), ordering=GeneratedOrdering("x_order", group_column_keys=("product.scope",)))
        self.assertEqual([row.scalar_values["x_order"] for row in actual], [10, 10, 20])

    def test_numeric_order_and_original_row_ties_do_not_change_original_values(self):
        values = (
            {"id": "a", "scope": "A", "quantity": "10", "price": "10"},
            {"id": "b", "scope": "A", "quantity": "10", "price": "2.75"},
            {"id": "c", "scope": "A", "quantity": "", "price": "2.750"},
            {"id": "d", "scope": "A", "quantity": "40", "price": ""},
        )
        for descending in (False, True):
            for blanks in ("first", "last"):
                with self.subTest(descending=descending, blanks=blanks):
                    self.exercise(values, ordering=GeneratedOrdering("x_order", group_column_keys=("product.scope",), sort_column_keys=("product.price",), sort_value_types=("decimal",), descending=descending, blanks=blanks))

    def test_numeric_order_is_exact_for_negatives_large_and_close_values(self):
        numbers = ("0", "-0.0", "-1", "-1.01", "-10", "-2", "+1.00", "1.00000000000000000000001", "1.00000000000000000000002", "100000000000000000000000000000000000000", "0.0000000000000000000000001")
        self.exercise(tuple({"id": f"row-{index}", "quantity": "10", "price": number} for index, number in enumerate(numbers)), ordering=GeneratedOrdering("x_order", sort_column_keys=("product.price",), sort_value_types=("decimal",)))

    def test_bad_ordering_values_are_blocked_instead_of_getting_arbitrary_ranks(self):
        self.exercise(({"id": "a", "quantity": "10", "price": "invalid"}, {"id": "b", "quantity": "10", "price": "2"}), ordering=GeneratedOrdering("x_order", sort_column_keys=("product.price",), sort_value_types=("decimal",)), expect_clean=False)
