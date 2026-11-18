"""Trace scalar inputs during authoritative evaluation without repeating its rules."""

from unittest import TestCase
from uuid import UUID

from impodo.domain.mapping.contracts import (
    ConcatenationBlankHandling,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleJoin,
    SelectionRuleSet,
    ValueMapping,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueRuleError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.recipe.value_rules import (
    ScalarTransformPolicy,
    compile_arithmetic_formula,
)

_ANCHOR = "column:anchor"
_LEFT = "column:left"
_RIGHT = "column:right"
_KEYS = {1: _ANCHOR, 2: _LEFT, 3: _RIGHT}


def _field(formula: str = "") -> ScalarFieldMapping:
    return ScalarFieldMapping(
        "name", _ANCHOR, transform=ScalarTransformPolicy(formula=formula)
    )


def _condition(index: int, key: str, *, number: bool = False) -> SelectionCondition:
    return SelectionCondition(
        str(UUID(int=index)),
        key,
        SelectionConditionOperator.EQUALS,
        "5" if number else "yes",
        value_type="integer" if number else "string",
    )


def _selection(*, invalid_second: bool = False) -> ScalarFieldMapping:
    return ScalarFieldMapping(
        "status",
        value_source=ScalarValueSource.CONDITIONAL_RULES,
        selection_rules=SelectionRuleSet(
            (
                SelectionRule(
                    str(UUID(int=101)),
                    (
                        _condition(1, _ANCHOR),
                        _condition(2, _LEFT, number=invalid_second),
                    ),
                    "first",
                    join=SelectionRuleJoin.ALL,
                ),
                SelectionRule(str(UUID(int=102)), (_condition(3, _RIGHT),), "second"),
            ),
        ),
    )


class ScalarSourceReadTests(TestCase):
    def test_multicolumn_formula_reports_only_evaluated_branch_names_as_stable_keys(
        self,
    ) -> None:
        mapping = _field("column_2 if column_1 else column_3")
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                True,
                source_values_by_ordinal={1: True, 2: "chosen", 3: "unused"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
            ),
            "chosen",
        )
        self.assertEqual(observed, [_ANCHOR, _LEFT])

    def test_ignored_authored_anchor_is_not_added_to_a_formula_read_trace(self) -> None:
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                _field("value if False else column_2"),
                "unused anchor",
                source_values_by_ordinal={2: "chosen"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
            ),
            "chosen",
        )
        self.assertEqual(observed, [_LEFT])

    def test_constant_formula_records_a_known_empty_source_read_set(self) -> None:
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                _field('"FIXED"'),
                "unused",
                source_values_by_ordinal={2: "unused too"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
            ),
            "FIXED",
        )
        self.assertEqual(observed, [])

    def test_compiled_arithmetic_preserves_operand_order_and_repeated_source_reads(
        self,
    ) -> None:
        expression = "column_2 + value * column_2"
        program = compile_arithmetic_formula(expression, allowed_names={"column_2"})
        assert program is not None
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                _field(expression),
                "2",
                source_values_by_ordinal={2: "3"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
                arithmetic_formula=program,
            ),
            "9",
        )
        self.assertEqual(observed, [_LEFT, _ANCHOR, _LEFT])

    def test_failed_formula_keeps_inputs_read_before_the_error(self) -> None:
        observed: list[str] = []
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _field("value / column_2 + column_3"),
                "1",
                source_values_by_ordinal={2: "0", 3: "unread"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
            )
        self.assertEqual(raised.exception.code, "SOURCE_FORMULA_INVALID")
        self.assertEqual(observed, [_ANCHOR, _LEFT])

    def test_fallback_blank_probe_records_the_raw_anchor_instead_of_the_literal(
        self,
    ) -> None:
        mapping = ScalarFieldMapping(
            "name",
            _ANCHOR,
            value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
            literal_value="Fallback",
            transform=ScalarTransformPolicy(
                trim=True, empty_as_null=True, formula="upper(value)"
            ),
        )
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping, "  ", source_read_observer=observed.append
            ),
            "FALLBACK",
        )
        self.assertEqual(set(observed), {_ANCHOR})
        observed.clear()
        mapping = ScalarFieldMapping(
            "name",
            _ANCHOR,
            value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
            literal_value="Fallback",
            transform=ScalarTransformPolicy(formula='"FIXED"'),
        )
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping, None, source_read_observer=observed.append
            ),
            "FIXED",
        )
        self.assertEqual(observed, [_ANCHOR])

    def test_inline_choice_records_the_raw_source_and_bypasses_the_formula(
        self,
    ) -> None:
        mapping = ScalarFieldMapping(
            "name",
            _ANCHOR,
            value_mappings=(ValueMapping("A", "Matched"),),
            transform=ScalarTransformPolicy(formula="value / column_2"),
        )
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                " A ",
                source_values_by_ordinal={2: "0"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
            ),
            "Matched",
        )
        self.assertEqual(observed, [_ANCHOR])

    def test_unmatched_inline_choice_retains_probe_and_formula_dependencies(
        self,
    ) -> None:
        mapping = ScalarFieldMapping(
            "name",
            _ANCHOR,
            value_mappings=(ValueMapping("A", "Matched"),),
            transform=ScalarTransformPolicy(formula="concat(value, column_2)"),
        )
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                "B",
                source_values_by_ordinal={2: "C"},
                source_column_keys_by_ordinal=_KEYS,
                source_read_observer=observed.append,
            ),
            "BC",
        )
        self.assertEqual(tuple(dict.fromkeys(observed)), (_ANCHOR, _LEFT))

    def test_concat_failure_records_only_the_prefix_that_was_inspected(self) -> None:
        mapping = ScalarFieldMapping(
            "name",
            value_source=ScalarValueSource.CONCATENATE,
            concatenation=ScalarConcatenation(
                (_ANCHOR, _LEFT, _RIGHT),
                blank_handling=ConcatenationBlankHandling.BLOCK_ROW,
            ),
        )
        observed: list[str] = []
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={_ANCHOR: "first", _LEFT: " ", _RIGHT: "not read"},
                source_read_observer=observed.append,
            )
        self.assertEqual(raised.exception.code, "SOURCE_CONCATENATION_PART_BLANK")
        self.assertEqual(observed, [_ANCHOR, _LEFT])

    def test_concat_skip_blank_records_all_contributing_and_blank_inputs_inspected(
        self,
    ) -> None:
        mapping = ScalarFieldMapping(
            "name",
            value_source=ScalarValueSource.CONCATENATE,
            concatenation=ScalarConcatenation((_ANCHOR, _LEFT, _RIGHT)),
        )
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={_ANCHOR: "first", _LEFT: None, _RIGHT: "last"},
                source_read_observer=observed.append,
            ),
            "first last",
        )
        self.assertEqual(observed, [_ANCHOR, _LEFT, _RIGHT])

    def test_selection_reads_every_condition_and_later_rule_for_overlap_evidence(
        self,
    ) -> None:
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                _selection(),
                None,
                source_values_by_key={_ANCHOR: "yes", _LEFT: "yes", _RIGHT: "yes"},
                source_read_observer=observed.append,
            ),
            "first",
        )
        self.assertEqual(observed, [_ANCHOR, _LEFT, _RIGHT])

    def test_selection_source_failure_keeps_the_exact_inspected_condition_prefix(
        self,
    ) -> None:
        observed: list[str] = []
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _selection(invalid_second=True),
                None,
                source_values_by_key={
                    _ANCHOR: "no",
                    _LEFT: "not an integer",
                    _RIGHT: "not read",
                },
                source_read_observer=observed.append,
            )
        self.assertEqual(raised.exception.code, "SOURCE_SELECTION_RULE_SOURCE_INVALID")
        self.assertEqual(observed, [_ANCHOR, _LEFT])

    def test_unresolved_selection_keeps_all_actual_reads_before_raising(self) -> None:
        observed: list[str] = []
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _selection(),
                None,
                source_values_by_key={_ANCHOR: "no", _LEFT: "no", _RIGHT: "no"},
                source_read_observer=observed.append,
            )
        self.assertEqual(raised.exception.code, "SOURCE_SELECTION_RULE_UNRESOLVED")
        self.assertEqual(observed, [_ANCHOR, _LEFT, _RIGHT])

    def test_constant_provider_has_no_original_source_reads(self) -> None:
        mapping = ScalarFieldMapping(
            "name",
            value_source=ScalarValueSource.CONSTANT,
            literal_value="Fixed",
            transform=ScalarTransformPolicy(formula="upper(value)"),
        )
        observed: list[str] = []
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping, None, source_read_observer=observed.append
            ),
            "FIXED",
        )
        self.assertEqual(observed, [])

    def test_blank_source_without_formula_is_still_an_observed_input(self) -> None:
        observed: list[str] = []
        self.assertIsNone(
            evaluate_scalar_mapping_value(
                _field(), None, source_read_observer=observed.append
            )
        )
        self.assertEqual(observed, [_ANCHOR])

    def test_missing_formula_key_binding_cannot_silently_drop_the_read(self) -> None:
        observed: list[str] = []
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _field("column_2"),
                "anchor",
                source_values_by_ordinal={2: "value"},
                source_read_observer=observed.append,
            )
        self.assertEqual(raised.exception.code, "SOURCE_FORMULA_INVALID")
        self.assertEqual(observed, [])
