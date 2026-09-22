"""Browser arithmetic reuse preserves prepared records and transformation evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport, compile_columnar_transformation_program,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping, IdentityComponentMapping, MappingDefinition,
    ScalarFieldMapping, ScalarValueSource, ValueMapping,
)
from impodo.domain.preparation.source import CompiledPreparedRowTransformer, SourceRow
from impodo.domain.recipe import value_rules
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.staging import evaluator
from impodo.domain.staging.evaluator import compile_browser_row_transformer
from impodo.domain.staging.transformation_impact import _TransformationImpactCollector
from impodo.domain.workspace.contracts import (
    SourceDataset, SourceDatasetColumn, SourceSelection,
)


def _fixture(model="x_custom.record", width=60):
    names = ("id", "amount", "series", *(f"unused_{i}" for i in range(width)))
    pairs = (
        (2.95, 1), (1, 3), (Decimal("2.9500"), "1.00"),
        (None, 1), (True, 1), ("1e3", 1), (2, 0),
        ("9999999999999999999999999999.99", "0.001"),
    )
    rows = tuple(SourceRow(number=i, values={
        "id": f"record-{i}", "amount": amount, "series": series,
        **{f"unused_{j}": "100" for j in range(width)},
    }) for i, (amount, series) in enumerate(pairs, 2))
    source = SourceDataset(
        dataset_id="dataset:arithmetic", name="records", row_count=len(rows),
        source=FileSourceBinding(
            file_id="file:arithmetic", table_key="sheet:records",
            source_sha256="sha256:" + "a" * 64, catalog_hash="sha256:" + "b" * 64,
            encoding=None, delimiter=None, header_row=1,
        ),
        columns=tuple(SourceDatasetColumn(i, name, f"source:{name}", "string")
                      for i, name in enumerate(names, 1)),
    )
    selection = SourceSelection(
        selection_id="selection:arithmetic", version=1, data_version_id="version:arithmetic",
        created_at=datetime(2026, 9, 15, tzinfo=timezone.utc), created_by="tester",
        datasets=(source,), content_hash="sha256:" + "c" * 64,
    )
    mapping = DatasetMapping(
        dataset_id=source.dataset_id, target_model=model,
        source_identity_column_keys=("source:id",),
        target_identity=(IdentityComponentMapping(("source:id",), ("x_code",)),),
        fields=(ScalarFieldMapping(
            target_field="x_result", source_column_key="source:amount", value_type="decimal",
            required=True, transform=ScalarTransformPolicy(
                formula="column_2 / column_3 * 1000", decimal_places=3,
            ),
        ),),
    )
    definition = MappingDefinition(
        mapping_id="mapping:arithmetic", schema_hash="sha256:" + "d" * 64,
        source_selection_hash=selection.content_hash, datasets=(mapping,),
    )
    return definition, selection, rows


def _prepare(definition, selection, rows):
    source = selection.datasets[0]
    transformer = compile_browser_row_transformer(source, source, definition.datasets[0], None, "source")
    plan = compile_browser_mapping(definition, selection).datasets[0]
    preparer = CompiledPreparedRowTransformer.compile(plan, transformer.headers)
    collector = _TransformationImpactCollector(definition.content_hash, detail_limit=100)
    records = []
    for row in rows:
        staged, issues = transformer.finish(transformer.project(row), impact_collector=collector)
        record = preparer.transform(staged)
        records.append(replace(record, issues=(*record.issues, *issues)))
    return tuple(records), collector.report()


class BrowserArithmeticFormulaTests(unittest.TestCase):
    def test_python_conversion_failure_never_exposes_internal_sentinel(self):
        definition, selection, rows = _fixture()
        field = replace(
            definition.datasets[0].fields[0],
            value_type="integer",
            transform=ScalarTransformPolicy(),
        )
        mapped = replace(
            definition,
            datasets=(
                replace(definition.datasets[0], fields=(field,)),
            ),
        )
        records, _report = _prepare(mapped, selection, rows[:1])

        issue = records[0].issues[0]
        self.assertEqual(issue.code, "SOURCE_TYPE_INVALID")
        self.assertEqual(
            issue.message,
            "The prepared value cannot be converted to integer.",
        )
        self.assertNotIn("__impodo", issue.message)

    def test_models_preserve_exact_records_issues_and_impacts(self):
        for model in ("sale.order.line", "account.move.line", "x_custom.record"):
            with self.subTest(model=model):
                definition, selection, rows = _fixture(model)
                with (
                    patch.object(value_rules, "compile_arithmetic_formula", return_value=None),
                    patch.object(evaluator, "compile_arithmetic_formula", return_value=None),
                ):
                    expected = _prepare(definition, selection, rows)
                actual = _prepare(definition, selection, rows)
                self.assertEqual(actual, expected)
                self.assertEqual(str(actual[0][0].scalar_values["x_result"]), "2950.000")
                self.assertTrue(any(issue.code == "SOURCE_FORMULA_INVALID" for record in actual[0] for issue in record.issues))

    def test_mapping_compiles_once_and_does_not_compile_during_row_execution(self):
        definition, selection, rows = _fixture()
        with patch.object(evaluator, "compile_arithmetic_formula", wraps=evaluator.compile_arithmetic_formula) as compiler:
            transformer = compile_browser_row_transformer(selection.datasets[0], selection.datasets[0], definition.datasets[0], None, "source")
        self.assertEqual(compiler.call_count, 1)
        with (
            patch.object(value_rules, "compile_arithmetic_formula", side_effect=AssertionError("row compilation")),
            patch.object(evaluator, "compile_arithmetic_formula", side_effect=AssertionError("row compilation")),
            patch.object(value_rules, "_eval_node", side_effect=AssertionError("row AST walk")),
        ):
            for row in rows:
                transformer.finish(transformer.project(row))

    def test_value_choice_bypass_and_provider_changes_preserve_formula_value(self):
        definition, selection, rows = _fixture()
        field = definition.datasets[0].fields[0]
        for provider in (ScalarValueSource.SOURCE, ScalarValueSource.CONSTANT, ScalarValueSource.SOURCE_WITH_FALLBACK):
            with self.subTest(provider=provider):
                configured = replace(field, value_source=provider, literal_value="4" if provider is not ScalarValueSource.SOURCE else None,
                                     transform=replace(field.transform, formula="value / column_3 * 1000"),
                                     value_mappings=(ValueMapping("2.95", "9.125"),))
                mapped = replace(definition, datasets=(replace(definition.datasets[0], fields=(configured,)),))
                with (
                    patch.object(value_rules, "compile_arithmetic_formula", return_value=None),
                    patch.object(evaluator, "compile_arithmetic_formula", return_value=None),
                ):
                    expected = _prepare(mapped, selection, rows)
                self.assertEqual(_prepare(mapped, selection, rows), expected)

    def test_formula_outside_subset_and_invalid_rule_preserve_row_behavior(self):
        definition, selection, rows = _fixture()
        for formula in ("abs(value)", "value if column_3 else 0", "column_999 + value", "value ** 2"):
            with self.subTest(formula=formula):
                mapped = replace(definition, datasets=(replace(definition.datasets[0], fields=(replace(definition.datasets[0].fields[0], transform=ScalarTransformPolicy(formula=formula)),)),))
                with (
                    patch.object(value_rules, "compile_arithmetic_formula", return_value=None),
                    patch.object(evaluator, "compile_arithmetic_formula", return_value=None),
                ):
                    expected = _prepare(mapped, selection, rows)
                self.assertEqual(_prepare(mapped, selection, rows), expected)

    def test_compiled_python_arithmetic_keeps_truthful_route_and_limit(self):
        definition, selection, _ = _fixture()
        decision = compile_columnar_transformation_program(definition, selection, selection.datasets[0].dataset_id)
        self.assertEqual(decision.support, ColumnarSupport.PYTHON_FALLBACK)
        self.assertIn("COLUMNAR_FORMULA_UNSUPPORTED", [reason.code for reason in decision.fallback_reasons])


if __name__ == "__main__":
    unittest.main()
