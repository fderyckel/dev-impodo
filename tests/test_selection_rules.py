from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from uuid import uuid4

import polars as pl
from starlette.datastructures import FormData

from impodo.adapters.polars_transformation import _provider_expression
from impodo.domain.compiler.columnar_transformation import (
    ColumnarInputColumn,
    ColumnarOperationKind,
    ColumnarScalarFieldProgram,
    ColumnarSelectionConditionProgram,
    ColumnarSelectionRuleProgram,
    ColumnarValueProviderProgram,
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleJoin,
    SelectionRuleSet,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueRuleError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.source_snapshot import source_value_column
from impodo.domain.source_snapshot import SOURCE_ROW_COLUMN
from impodo.web.presenters.mapping_forms import _selection_rules_from_form


HASH_A = "sha256:" + "a" * 64


def _condition(
    source: str,
    operator: SelectionConditionOperator,
    value: str | None,
) -> SelectionCondition:
    return SelectionCondition(
        condition_id=str(uuid4()),
        source_column_key=source,
        operator=operator,
        comparison_value=value,
    )


def _mapping(*, otherwise: str | None = "person") -> ScalarFieldMapping:
    return ScalarFieldMapping(
        target_field="company_type",
        value_source=ScalarValueSource.CONDITIONAL_RULES,
        value_type="string",
        categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
        selection_rules=SelectionRuleSet(
            rules=(
                SelectionRule(
                    rule_id=str(uuid4()),
                    join=SelectionRuleJoin.ALL,
                    conditions=(
                        _condition(
                            "column:organisation",
                            SelectionConditionOperator.IS_NOT_BLANK,
                            None,
                        ),
                        _condition(
                            "column:category",
                            SelectionConditionOperator.EQUALS_CASEFOLD,
                            "business",
                        ),
                    ),
                    target_value="company",
                ),
            ),
            otherwise_value=otherwise,
        ),
    )


class SelectionRuleTests(unittest.TestCase):
    def test_first_matching_rule_uses_multiple_columns_and_otherwise(self) -> None:
        mapping = _mapping()

        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={
                    "column:organisation": "Acme",
                    "column:category": "BUSINESS",
                },
            ),
            "company",
        )
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={
                    "column:organisation": "",
                    "column:category": "business",
                },
            ),
            "person",
        )

    def test_unmatched_rule_without_otherwise_blocks_the_row(self) -> None:
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _mapping(otherwise=None),
                None,
                source_values_by_key={
                    "column:organisation": "",
                    "column:category": "consumer",
                },
            )
        self.assertEqual(raised.exception.code, "SOURCE_SELECTION_RULE_UNRESOLVED")

    def test_contract_v12_round_trips_rules_without_changing_order(self) -> None:
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=HASH_A,
            schema_hash=HASH_A,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:0123456789abcdef01234567",
                    target_model="res.partner",
                    fields=(_mapping(),),
                ),
            ),
        )

        restored = MappingDefinition.from_json(definition.to_json())

        self.assertEqual(restored, definition)
        self.assertEqual(restored.contract_version, 12)
        with self.assertRaisesRegex(ValueError, "require version 12"):
            MappingDefinition(
                mapping_id=definition.mapping_id,
                source_selection_hash=HASH_A,
                schema_hash=HASH_A,
                datasets=definition.datasets,
                contract_version=11,
            )

    def test_form_parser_rejects_extra_fields_and_accepts_portable_rules(self) -> None:
        definition = MappingDefinition(
                mapping_id=str(uuid4()),
                source_selection_hash=HASH_A,
                schema_hash=HASH_A,
                datasets=(
                    DatasetMapping(
                        dataset_id="dataset:0123456789abcdef01234567",
                        target_model="res.partner",
                        fields=(_mapping(),),
                    ),
                ),
            )
        payload = json.loads(definition.to_json())["datasets"][0]["fields"][0][
            "selection_rules"
        ]
        parsed = _selection_rules_from_form(
            FormData((("rules", json.dumps(payload)),)),
            "rules",
        )
        self.assertEqual(parsed, definition.datasets[0].fields[0].selection_rules)
        payload["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "invalid"):
            _selection_rules_from_form(
                FormData((("rules", json.dumps(payload)),)),
                "rules",
            )

    def test_native_provider_is_set_based_and_matches_rule_order(self) -> None:
        organisation = ColumnarInputColumn(
            stable_key="column:organisation",
            ordinal=1,
            source_name="Organisation",
            candidate_type="string",
        )
        category = ColumnarInputColumn(
            stable_key="column:category",
            ordinal=2,
            source_name="Category",
            candidate_type="string",
        )
        provider = ColumnarValueProviderProgram(
            operation=ColumnarOperationKind.CONDITIONAL_SELECTION,
            source=None,
            literal_value=None,
            value_mappings=(),
            selection_rules=(
                ColumnarSelectionRuleProgram(
                    conditions=(
                        ColumnarSelectionConditionProgram(
                            organisation,
                            "is_not_blank",
                            None,
                            "string",
                        ),
                        ColumnarSelectionConditionProgram(
                            category,
                            "equals_casefold",
                            "business",
                            "string",
                        ),
                    ),
                    target_value="company",
                    join="all",
                ),
            ),
            selection_otherwise_value="person",
        )
        field = SimpleNamespace(provider=provider)
        proposed, _matched = _provider_expression(field)
        frame = pl.DataFrame(
            {
                SOURCE_ROW_COLUMN: [1, 2],
                source_value_column(1): ["Acme", ""],
                source_value_column(2): ["BUSINESS", "business"],
            }
        )

        self.assertEqual(
            frame.select(proposed.alias("choice"))["choice"].to_list(),
            ["company", "person"],
        )

    def test_complete_columnar_program_projects_each_rule_input_once(self) -> None:
        from tests.test_columnar_compiler import _selection, _supported_definition

        selection = _selection()
        definition = _supported_definition(selection)
        dataset = definition.datasets[0]
        conditional = ScalarFieldMapping(
            target_field="category",
            value_source=ScalarValueSource.CONDITIONAL_RULES,
            selection_rules=SelectionRuleSet(
                rules=(
                    SelectionRule(
                        rule_id=str(uuid4()),
                        conditions=(
                            _condition(
                                "product.name",
                                SelectionConditionOperator.CONTAINS,
                                "Ltd",
                            ),
                            _condition(
                                "product.quantity",
                                SelectionConditionOperator.IS_NOT_BLANK,
                                None,
                            ),
                        ),
                        target_value="company",
                    ),
                ),
                otherwise_value="person",
            ),
        )
        fields = tuple(
            conditional if field.target_field == "category" else field
            for field in dataset.fields
        )
        definition = replace(
            definition,
            datasets=(replace(dataset, fields=fields),),
        )

        decision = compile_columnar_transformation_program(
            definition,
            selection,
            dataset.dataset_id,
        )

        self.assertEqual(decision.support.value, "supported")
        program = decision.program
        assert program is not None
        category = next(
            field for field in program.scalar_fields if field.target_field == "category"
        )
        self.assertEqual(
            category.provider.operation,
            ColumnarOperationKind.CONDITIONAL_SELECTION,
        )
        self.assertEqual(
            {
                condition.source.stable_key
                for rule in category.provider.selection_rules
                for condition in rule.conditions
            },
            {"product.name", "product.quantity"},
        )
        self.assertEqual(
            program.from_portable_dict(program.to_portable_dict()),
            program,
        )


if __name__ == "__main__":
    unittest.main()
