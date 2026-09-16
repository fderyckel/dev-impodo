from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from uuid import uuid4

import polars as pl

from impodo.adapters.polars_transformation import _selection_condition_expression

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    RowInclusionCondition,
    RowInclusionJoin,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.row_inclusion import row_is_included
from impodo.domain.mapping.source_conditions import (
    SourceConditionValueError,
    source_condition_configuration_problems,
)
from impodo.domain.source_snapshot import source_value_column
from impodo.domain.mapping.validation.row_inclusion import (
    _validate_row_inclusion,
)


HASH = "sha256:" + "1" * 64


def _condition(
    *,
    source: str = "column:status",
    operator: SelectionConditionOperator = SelectionConditionOperator.EQUALS,
    value: str | None = "30",
    value_type: str = "string",
) -> RowInclusionCondition:
    return RowInclusionCondition(
        condition_id=str(uuid4()),
        source_column_key=source,
        operator=operator,
        comparison_value=value,
        value_type=value_type,
    )


class RowInclusionContractTests(unittest.TestCase):
    def test_default_policy_includes_every_row(self) -> None:
        self.assertTrue(row_is_included(RowInclusionPolicy(), {}))

    def test_exact_text_condition_includes_only_status_30(self) -> None:
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            conditions=(_condition(),),
        )

        self.assertTrue(
            row_is_included(policy, {"column:status": "30"})
        )
        self.assertTrue(row_is_included(policy, {"column:status": 30}))
        self.assertFalse(
            row_is_included(policy, {"column:status": "20"})
        )
        self.assertFalse(
            row_is_included(policy, {"column:status": " 30 "})
        )
        self.assertFalse(row_is_included(policy, {"column:status": None}))

    def test_exact_list_excludes_only_named_nonblank_values_in_both_evaluators(
        self,
    ) -> None:
        value = "BOM A, BOM B"
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            conditions=(
                _condition(
                    source="column:bom",
                    operator=SelectionConditionOperator.NOT_IN,
                    value=value,
                ),
            ),
        )
        inputs = ("BOM A", "BOM B", "BOM C", "", None)
        expected = (False, False, True, False, False)
        self.assertEqual(
            tuple(row_is_included(policy, {"column:bom": item}) for item in inputs),
            expected,
        )
        column = source_value_column(1)
        program = SimpleNamespace(
            source=SimpleNamespace(ordinal=1),
            operator="not_in",
            comparison_value=value,
            value_type="string",
        )
        actual = (
            pl.DataFrame({column: inputs})
            .select(_selection_condition_expression(program).alias("included"))
            .get_column("included")
            .to_list()
        )
        self.assertEqual(actual, list(expected))

    def test_exact_list_rejects_ambiguous_or_nontext_configuration(self) -> None:
        for value in ("", "BOM A,", "BOM A, BOM A"):
            problems = source_condition_configuration_problems(
                operator=SelectionConditionOperator.NOT_IN,
                comparison_value=value,
                value_type="string",
            )
            self.assertIn("value", {problem.kind for problem in problems})
        problems = source_condition_configuration_problems(
            operator=SelectionConditionOperator.NOT_IN,
            comparison_value="1,2",
            value_type="integer",
        )
        self.assertIn("operator", {problem.kind for problem in problems})

    def test_all_and_any_join_have_explicit_semantics(self) -> None:
        conditions = (
            _condition(),
            _condition(
                source="column:company",
                value="BE",
            ),
        )
        values = {"column:status": "30", "column:company": "FR"}

        self.assertFalse(
            row_is_included(
                RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=conditions,
                    join=RowInclusionJoin.ALL,
                ),
                values,
            )
        )
        self.assertTrue(
            row_is_included(
                RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=conditions,
                    join=RowInclusionJoin.ANY,
                ),
                values,
            )
        )

    def test_unparseable_typed_value_is_not_silently_excluded(self) -> None:
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            conditions=(
                _condition(value="30", value_type="integer"),
            ),
        )

        with self.assertRaises(SourceConditionValueError):
            row_is_included(policy, {"column:status": "unknown"})

    def test_current_contract_round_trips_and_hashes_rule_meaning(self) -> None:
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            conditions=(_condition(),),
        )
        definition = MappingDefinition(
            mapping_id="mapping:products",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:products",
                    target_model="product.template",
                    row_inclusion=policy,
                ),
            ),
        )

        restored = MappingDefinition.from_json(definition.to_json())

        self.assertEqual(restored, definition)
        self.assertEqual(restored.contract_version, 17)
        self.assertEqual(
            definition.to_dict()["datasets"][0]["row_inclusion"]["mode"],
            "matching_rows",
        )
        changed = replace(
            policy,
            conditions=(replace(policy.conditions[0], comparison_value="40"),),
        )
        self.assertNotEqual(
            definition.content_hash,
            replace(
                definition,
                datasets=(
                    replace(definition.datasets[0], row_inclusion=changed),
                ),
            ).content_hash,
        )

    def test_v15_layout_omits_new_field_and_rejects_new_meaning(self) -> None:
        legacy = MappingDefinition(
            mapping_id="mapping:products",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:products",
                    target_model="product.template",
                ),
            ),
            contract_version=15,
        )

        payload = legacy.to_dict()

        self.assertNotIn("row_inclusion", payload["datasets"][0])
        self.assertEqual(MappingDefinition.from_dict(payload), legacy)
        with self.assertRaisesRegex(ValueError, "cannot contain row inclusion"):
            replace(
                legacy,
                datasets=(
                    replace(
                        legacy.datasets[0],
                        row_inclusion=RowInclusionPolicy(
                            mode=RowInclusionMode.MATCHING_ROWS,
                            conditions=(_condition(),),
                        ),
                    ),
                ),
            )

    def test_policy_shapes_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot contain conditions"):
            RowInclusionPolicy(conditions=(_condition(),))
        with self.assertRaisesRegex(ValueError, "requires one to 8"):
            RowInclusionPolicy(mode=RowInclusionMode.MATCHING_ROWS)
        duplicate = _condition()
        with self.assertRaisesRegex(ValueError, "identifiers must be unique"):
            RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(duplicate, duplicate),
            )

    def test_semantic_validation_reports_unknown_columns_and_bad_literals(
        self,
    ) -> None:
        dataset = DatasetMapping(
            dataset_id="dataset:products",
            target_model="product.template",
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    _condition(
                        source="column:missing",
                        value="not-a-number",
                        value_type="integer",
                    ),
                ),
            ),
        )
        issues = []

        _validate_row_inclusion(
            dataset,
            "/datasets/0",
            {
                "column:status": SimpleNamespace(
                    stable_key="column:status",
                    candidate_type="string",
                )
            },
            issues,
        )

        self.assertEqual(
            [item.code for item in issues],
            [
                "MAPPING_SOURCE_COLUMN_UNKNOWN",
                "MAPPING_ROW_INCLUSION_VALUE_INVALID",
            ],
        )

    def test_columnar_compiler_routes_rule_to_proven_row_evaluator(self) -> None:
        definition = MappingDefinition(
            mapping_id="mapping:products",
            source_selection_hash=HASH,
            schema_hash=HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:products",
                    target_model="product.template",
                    row_inclusion=RowInclusionPolicy(
                        mode=RowInclusionMode.MATCHING_ROWS,
                        conditions=(_condition(),),
                    ),
                ),
            ),
        )

        selection = SimpleNamespace(
            content_hash=HASH,
            datasets=(
                SimpleNamespace(
                    dataset_id="dataset:products",
                    name="products",
                    columns=(
                        SimpleNamespace(
                            stable_key="column:status",
                            ordinal=1,
                            source_name="Code statut product",
                            candidate_type="string",
                        ),
                    ),
                ),
            ),
        )

        decision = compile_columnar_transformation_program(
            definition,
            selection,
            "dataset:products",
        )

        self.assertIs(decision.support, ColumnarSupport.PYTHON_FALLBACK)
        self.assertEqual(
            [reason.code for reason in decision.fallback_reasons],
            ["COLUMNAR_ROW_INCLUSION_UNSUPPORTED"],
        )


if __name__ == "__main__":
    unittest.main()
