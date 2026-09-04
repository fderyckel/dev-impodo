from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from uuid import uuid4

import polars as pl
from starlette.datastructures import FormData

from impodo.adapters.polars_transformation import (
    _aggregate_rule_observations,
    _compile_rule_observations,
    _execution_layout,
    _provider_expression,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarInputColumn,
    ColumnarOperationKind,
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
from impodo.domain.staging.transformation_impact import (
    selection_rule_impact_definitions,
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
                            SelectionConditionOperator.EQUALS_IGNORE_CASE,
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

    def test_invalid_typed_source_value_blocks_instead_of_using_otherwise(self) -> None:
        mapping = ScalarFieldMapping(
            target_field="company_type",
            value_source=ScalarValueSource.CONDITIONAL_RULES,
            selection_rules=SelectionRuleSet(
                rules=(
                    SelectionRule(
                        rule_id=str(uuid4()),
                        conditions=(
                            SelectionCondition(
                                condition_id=str(uuid4()),
                                source_column_key="column:employees",
                                operator=SelectionConditionOperator.GREATER_THAN,
                                comparison_value="5",
                                value_type="integer",
                            ),
                        ),
                        target_value="company",
                    ),
                ),
                otherwise_value="person",
            ),
        )
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={"column:employees": "unknown"},
            )
        self.assertEqual(
            raised.exception.code,
            "SOURCE_SELECTION_RULE_SOURCE_INVALID",
        )

    def test_rule_observer_reports_first_match_and_every_overlap(self) -> None:
        first = _mapping()
        assert first.selection_rules is not None
        mapping = replace(
            first,
            selection_rules=SelectionRuleSet(
                rules=(
                    first.selection_rules.rules[0],
                    SelectionRule(
                        rule_id=str(uuid4()),
                        conditions=(
                            _condition(
                                "column:category",
                                SelectionConditionOperator.EQUALS_IGNORE_CASE,
                                "business",
                            ),
                        ),
                        target_value="person",
                    ),
                ),
                otherwise_value="person",
            ),
        )
        observed: list[tuple[int, bool, bool, bool]] = []

        proposed = evaluate_scalar_mapping_value(
            mapping,
            None,
            source_values_by_key={
                "column:organisation": "Acme",
                "column:category": "BUSINESS",
            },
            selection_rule_observer=(
                lambda index, matched, selected, overlap: observed.append(
                    (index, matched, selected, overlap)
                )
            ),
        )

        self.assertEqual(proposed, "company")
        self.assertEqual(
            observed,
            [(0, True, True, True), (1, True, False, True)],
        )
        definitions = selection_rule_impact_definitions("dataset:contacts", mapping)
        self.assertEqual(
            [item.rule_kind for item in definitions],
            [
                "selection_rule",
                "selection_rule_overlap",
                "selection_rule",
                "selection_rule_overlap",
            ],
        )
        zero_match = replace(
            definitions[2],
            evaluated_value_count=3,
            matched_value_count=0,
            changed_value_count=0,
        )
        overlap = replace(
            definitions[1],
            evaluated_value_count=3,
            matched_value_count=1,
            changed_value_count=1,
        )
        self.assertEqual(zero_match.acknowledgement_reason, "zero_match")
        self.assertEqual(overlap.acknowledgement_reason, "overlap")

    def test_contract_v14_round_trips_rules_without_changing_order(self) -> None:
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
        self.assertEqual(restored.contract_version, 15)
        legacy = MappingDefinition(
            mapping_id=definition.mapping_id,
            source_selection_hash=HASH_A,
            schema_hash=HASH_A,
            datasets=definition.datasets,
            contract_version=13,
        )
        self.assertEqual(
            MappingDefinition.from_json(legacy.to_json()),
            legacy,
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
                            "equals_ignore_case",
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
        from tests.domain.recipe.test_columnar_compiler import _selection, _supported_definition

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

    def test_native_rule_evidence_counts_priority_and_overlap_set_wise(self) -> None:
        from tests.domain.recipe.test_columnar_compiler import _selection, _supported_definition

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
                        ),
                        target_value="company",
                    ),
                    SelectionRule(
                        rule_id=str(uuid4()),
                        conditions=(
                            _condition(
                                "product.quantity",
                                SelectionConditionOperator.IS_NOT_BLANK,
                                None,
                            ),
                        ),
                        target_value="person",
                    ),
                ),
                otherwise_value="person",
            ),
        )
        definition = replace(
            definition,
            datasets=(
                replace(
                    dataset,
                    fields=tuple(
                        conditional if field.target_field == "category" else field
                        for field in dataset.fields
                    ),
                ),
            ),
        )
        decision = compile_columnar_transformation_program(
            definition,
            selection,
            dataset.dataset_id,
        )
        program = decision.program
        assert program is not None
        observations, expressions = _compile_rule_observations(
            program,
            _execution_layout(program),
        )
        values = {
            SOURCE_ROW_COLUMN: [1, 2, 3],
            **{
                source_value_column(item.ordinal): (
                    ["Acme Ltd", "Solo Ltd", "Person"]
                    if item.stable_key == "product.name"
                    else ["2", "", "1"]
                    if item.stable_key == "product.quantity"
                    else ["", "", ""]
                )
                for item in program.inputs
            },
        }
        frame = pl.DataFrame(values).with_columns(expressions)
        impacts = _aggregate_rule_observations(frame, observations)
        selection_impacts = sorted(
            (
                item
                for item in impacts.values()
                if item.target_field == "category"
                and item.rule_kind.startswith("selection_rule")
            ),
            key=lambda item: item.rule_fingerprint,
        )

        self.assertEqual(len(selection_impacts), 4)
        self.assertEqual(
            {item.rule_fingerprint for item in selection_impacts},
            {
                item.rule_fingerprint
                for item in selection_rule_impact_definitions(
                    dataset.dataset_id,
                    conditional,
                )
            },
        )
        self.assertEqual(
            sorted(
                (
                    item.rule_kind,
                    item.matched_value_count,
                    item.changed_value_count,
                )
                for item in selection_impacts
            ),
            [
                ("selection_rule", 2, 1),
                ("selection_rule", 2, 2),
                ("selection_rule_overlap", 1, 1),
                ("selection_rule_overlap", 1, 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
