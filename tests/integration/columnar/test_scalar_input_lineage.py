"""Native publication and reopened projection reconstruct identical scalar proof."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import duckdb

from impodo.adapters.duckdb.native_prepared_projection import projected_encoded_rows_sql
from impodo.adapters.polars_transformation import iter_polars_prepared_batches
from impodo.domain.compiler.columnar_transformation import (
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.contracts import (
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleSet,
)
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.staging.canonical_projection import canonical_prepared_session_row
from impodo.domain.staging.preparation_session import (
    PreparedCanonicalProjection,
    native_scalar_input_keys,
)
from tests.integration.columnar import test_polars_transformation as fixtures


class NativeScalarInputLineageTests(unittest.TestCase):
    def test_native_hash_json_and_reconstructed_rows_bind_the_same_input_proof(self):
        selection = fixtures._selection()
        definition = fixtures._definition(selection)
        fields = (
            ScalarFieldMapping(
                "joined",
                value_source=ScalarValueSource.CONCATENATE,
                concatenation=ScalarConcatenation(("product.scope", "product.name")),
            ),
            ScalarFieldMapping(
                "choice",
                value_source=ScalarValueSource.CONDITIONAL_RULES,
                selection_rules=SelectionRuleSet(
                    (
                        SelectionRule(
                            "11111111-1111-4111-8111-111111111111",
                            (
                                SelectionCondition(
                                    "22222222-2222-4222-8222-222222222222",
                                    "product.category",
                                    SelectionConditionOperator.IS_NOT_BLANK,
                                ),
                                SelectionCondition(
                                    "33333333-3333-4333-8333-333333333333",
                                    "product.name",
                                    SelectionConditionOperator.IS_NOT_BLANK,
                                ),
                            ),
                            "chosen",
                        ),
                    ),
                    otherwise_value="other",
                ),
            ),
            ScalarFieldMapping("direct", "product.sku"),
        )
        definition = replace(
            definition,
            datasets=(
                replace(definition.datasets[0], fields=fields, relationships=()),
            ),
        )
        decision = compile_columnar_transformation_program(
            definition, selection, fixtures.DATASET_ID
        )
        self.assertIsNotNone(decision.program)
        program = decision.program
        assert program is not None
        inputs = native_scalar_input_keys(program)
        self.assertEqual(
            inputs,
            {
                "joined": ("product.scope", "product.name"),
                "choice": ("product.name", "product.category"),
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path, source = fixtures._write_snapshot(
                root, selection, fixtures._rows()[:3]
            )
            prepared_path, prepared = fixtures._write_prepared_snapshot(
                root, source_path, source, program
            )
            projection = PreparedCanonicalProjection(
                dataset_id=program.dataset_id,
                dataset=program.dataset_name,
                ordinal_start=0,
                row_count=prepared.row_count,
                mode=program.target_mode,
                source_hash=source.content_hash,
                physical_dataset_id=program.dataset_id,
                field_sources={
                    "joined": ("product.scope", "product.name"),
                    "choice": ("product.category", "product.name"),
                    "direct": ("product.sku",),
                },
                program=program,
                set_based_projection=True,
                field_input_keys=inputs,
            )
            restored = PreparedCanonicalProjection.from_portable_dict(
                json.loads(json.dumps(projection.to_portable_dict()))
            )
            self.assertEqual(restored, projection)
            expected = []
            for batch in iter_polars_prepared_batches(
                prepared_path, prepared, source, program, materialize_records=False
            ):
                for parts in zip(
                    batch.source_rows,
                    batch.source_identities,
                    batch.target_identities,
                    batch.target_scopes,
                    batch.scalar_values,
                    batch.references,
                    batch.issues,
                    strict=True,
                ):
                    source_row, identity, target, scope, values, references, issues = (
                        parts
                    )
                    encoded = canonical_prepared_session_row(
                        dataset=projection.dataset,
                        source_row=source_row,
                        target_model=program.target_model,
                        source_identity=identity,
                        target_identity=target,
                        target_scope=scope,
                        scalar_values=values,
                        references=references,
                        issues=issues,
                        ordinal=len(expected),
                        mode=projection.mode,
                        source_hash=projection.source_hash,
                        source_selection_hash=program.source_selection_hash,
                        mapping_hash=program.mapping_content_hash,
                        schema_hash=program.schema_hash,
                        field_sources=projection.field_sources,
                        physical_dataset_id=projection.physical_dataset_id,
                        field_input_keys=restored.field_input_keys,
                    )
                    expected.append(
                        CanonicalRow.from_dict(json.loads(encoded.row_json))
                    )
            with duckdb.connect(":memory:") as connection:
                result = connection.execute(
                    projected_encoded_rows_sql(
                        restored, prepared_ordinals=tuple(range(prepared.row_count))
                    ),
                    [str(prepared_path), *range(prepared.row_count)],
                ).fetchall()
            actual = tuple(
                CanonicalRow.from_dict(json.loads(str(row[-1]))) for row in result
            )
            self.assertEqual(actual, tuple(expected))
            self.assertTrue(
                all(row.lineage.field_input_keys == inputs for row in actual)
            )
            self.assertEqual(
                tuple(row.row_id for row in actual),
                tuple(row.row_id for row in expected),
            )

    def test_absent_native_proof_keeps_the_current_projection_payload_unchanged(self):
        selection = fixtures._selection()
        program = compile_columnar_transformation_program(
            fixtures._definition(selection), selection, fixtures.DATASET_ID
        ).program
        assert program is not None
        projection = PreparedCanonicalProjection(
            dataset_id=program.dataset_id,
            dataset=program.dataset_name,
            ordinal_start=0,
            row_count=4,
            mode=program.target_mode,
            source_hash=fixtures.HASH_A,
            physical_dataset_id=program.dataset_id,
            field_sources={},
            program=program,
        )
        self.assertNotIn("field_input_keys", projection.to_portable_dict())
        self.assertEqual(
            PreparedCanonicalProjection.from_portable_dict(
                projection.to_portable_dict()
            ),
            projection,
        )
