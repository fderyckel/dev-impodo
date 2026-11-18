"""Trace formula source reads without changing current formula evaluation."""

from decimal import Decimal, DivisionByZero
from unittest import TestCase

from impodo.domain.recipe.value_rules import (
    FormulaValidationError,
    ScalarRuleError,
    ScalarTransformPolicy,
    compile_arithmetic_formula,
    evaluate_formula,
    evaluate_reference_formula,
    prepare_rule_text,
)


class _Unrenderable:
    def __str__(self) -> str:
        raise ValueError("cannot render this source scalar")


class FormulaReadObserverTests(TestCase):
    def test_conditional_reports_the_test_and_selected_branch_only(self) -> None:
        observed: list[str] = []
        result = evaluate_formula(
            "left if condition else right",
            {"left": "unused", "condition": False, "right": "selected"},
            read_observer=observed.append,
        )
        self.assertEqual(result, "selected")
        self.assertEqual(observed, ["condition", "right"])

    def test_literal_branch_has_no_source_reads_even_with_an_unused_context(
        self,
    ) -> None:
        observed: list[str] = []
        self.assertEqual(
            evaluate_formula(
                'value if False else "FIXED"',
                {"value": "007", "column_2": "unused"},
                read_observer=observed.append,
            ),
            "FIXED",
        )
        self.assertEqual(observed, [])

    def test_nested_conditionals_report_only_the_executed_names(self) -> None:
        observed: list[str] = []
        result = evaluate_formula(
            'concat("prefix/", left if flag else right)',
            {"left": "unused", "right": "chosen", "flag": False},
            read_observer=observed.append,
        )
        self.assertEqual(result, "prefix/chosen")
        self.assertEqual(observed, ["flag", "right"])

    def test_chained_comparison_stops_before_the_unused_operand(self) -> None:
        observed: list[str] = []
        result = evaluate_formula(
            "left < middle < right",
            {"left": 3, "middle": 2, "right": "unused"},
            read_observer=observed.append,
        )
        self.assertFalse(result)
        self.assertEqual(observed, ["left", "middle"])

    def test_boolean_operands_keep_the_existing_eager_interpreter_behavior(
        self,
    ) -> None:
        for expression, expected in (
            ("True or value", True),
            ("False and value", False),
        ):
            observed: list[str] = []
            with self.subTest(expression=expression):
                self.assertEqual(
                    evaluate_formula(
                        expression, {"value": "read"}, read_observer=observed.append
                    ),
                    expected,
                )
                self.assertEqual(observed, ["value"])

    def test_helper_arguments_are_eager_and_function_names_are_not_source_reads(
        self,
    ) -> None:
        observed: list[str] = []
        self.assertEqual(
            evaluate_formula(
                "coalesce(first, upper(second))",
                {"first": "chosen", "second": "also read"},
                read_observer=observed.append,
            ),
            "chosen",
        )
        self.assertEqual(observed, ["first", "second"])

    def test_compiled_arithmetic_reports_executed_operands_including_repeats(
        self,
    ) -> None:
        program = compile_arithmetic_formula(
            "left + right * left", allowed_names={"left", "right"}
        )
        assert program is not None
        observed: list[str] = []
        self.assertEqual(
            program.evaluate(
                {"left": "2", "right": "3", "unused": "ignored"},
                read_observer=observed.append,
            ),
            Decimal(8),
        )
        self.assertEqual(observed, ["left", "right", "left"])

    def test_arithmetic_error_does_not_report_later_unused_operands(self) -> None:
        observed: list[str] = []
        with self.assertRaises(DivisionByZero):
            evaluate_formula(
                "left / zero + later",
                {"left": "1", "zero": "0", "later": "9"},
                read_observer=observed.append,
            )
        self.assertEqual(observed, ["left", "zero"])

    def test_failed_eager_context_coercion_retains_only_the_failing_input(self) -> None:
        observed: list[str] = []
        with self.assertRaisesRegex(ValueError, "cannot render"):
            evaluate_formula(
                'value if False else "FIXED"',
                {"unobserved": "007", "failing": _Unrenderable(), "value": "unused"},
                read_observer=observed.append,
            )
        self.assertEqual(observed, ["failing"])

    def test_failed_compiled_input_coercion_is_reported_before_propagating(
        self,
    ) -> None:
        program = compile_arithmetic_formula(
            "left + right", allowed_names={"left", "right"}
        )
        assert program is not None
        observed: list[str] = []
        with self.assertRaisesRegex(ValueError, "cannot render"):
            program.evaluate(
                {"left": _Unrenderable(), "right": "2"}, read_observer=observed.append
            )
        self.assertEqual(observed, ["left"])

    def test_missing_compiled_input_is_reported_with_the_original_error(self) -> None:
        program = compile_arithmetic_formula(
            "left + right", allowed_names={"left", "right"}
        )
        assert program is not None
        observed: list[str] = []
        with self.assertRaises(KeyError) as raised:
            program.evaluate({"left": "1"}, read_observer=observed.append)
        self.assertEqual(raised.exception.args, ("right",))
        self.assertEqual(observed, ["right"])

    def test_invalid_formula_produces_no_read_events(self) -> None:
        observed: list[str] = []
        with self.assertRaises(FormulaValidationError):
            evaluate_formula("value +", {"value": "1"}, read_observer=observed.append)
        self.assertEqual(observed, [])

    def test_text_preparation_propagates_formula_reads_without_reading_raw_by_default(
        self,
    ) -> None:
        observed: list[str] = []
        self.assertEqual(
            prepare_rule_text(
                "raw",
                ScalarTransformPolicy(
                    formula='concat(value, "/", column_2)', case_mode="uppercase"
                ),
                formula_context={"column_2": "extra"},
                formula_read_observer=observed.append,
            ),
            "RAW/EXTRA",
        )
        self.assertEqual(observed, ["value", "column_2"])
        observed.clear()
        self.assertEqual(
            prepare_rule_text(
                "raw", ScalarTransformPolicy(), formula_read_observer=observed.append
            ),
            "raw",
        )
        self.assertEqual(observed, [])

    def test_text_preparation_propagates_compiled_reads_and_retains_error_classification(
        self,
    ) -> None:
        expression = "column_2 / value"
        program = compile_arithmetic_formula(expression, allowed_names={"column_2"})
        assert program is not None
        observed: list[str] = []
        with self.assertRaises(ScalarRuleError) as raised:
            prepare_rule_text(
                "0",
                ScalarTransformPolicy(formula=expression),
                formula_context={"column_2": "1"},
                arithmetic_formula=program,
                formula_read_observer=observed.append,
            )
        self.assertEqual(raised.exception.code, "SOURCE_FORMULA_INVALID")
        self.assertEqual(observed, ["column_2", "value"])

    def test_reference_formula_observer_preserves_exact_source_text(self) -> None:
        observed: list[str] = []
        self.assertEqual(
            evaluate_reference_formula(
                'concat(value, "/", column_2)',
                {"value": "001", "column_2": "002"},
                read_observer=observed.append,
            ),
            "001/002",
        )
        self.assertEqual(observed, ["value", "column_2"])

    def test_observing_reads_preserves_result_types_values_and_error_messages(
        self,
    ) -> None:
        cases = (
            ("value * 2", {"value": "2.500"}),
            ("column_2 / value", {"value": "0", "column_2": "1"}),
            ('value if flag else "FIXED"', {"value": "unused", "flag": False}),
            ("left < middle < right", {"left": 3, "middle": 2, "right": 1}),
            ("coalesce(value, column_2)", {"value": None, "column_2": "chosen"}),
            ("value +", {"value": "invalid"}),
        )
        for expression, context in cases:
            with self.subTest(expression=expression):
                expected = self._formula_outcome(expression, context)
                self.assertEqual(
                    self._formula_outcome(expression, context, observed=[]), expected
                )

    @staticmethod
    def _formula_outcome(expression, context, *, observed=None):
        try:
            value = evaluate_formula(
                expression,
                context,
                read_observer=observed.append if observed is not None else None,
            )
            return type(value), value
        except (ArithmeticError, TypeError, ValueError) as error:
            return type(error), str(error)
