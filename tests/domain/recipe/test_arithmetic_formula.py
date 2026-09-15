"""Exact arithmetic parity and reuse of compiled formula work."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, localcontext
import random
import unittest
from unittest.mock import patch

from impodo.domain.recipe import value_rules
from impodo.domain.recipe.value_rules import (
    FormulaValidationError,
    compile_arithmetic_formula,
    evaluate_formula,
    validate_formula,
)


def _oracle(expression, context):
    parsed = validate_formula(expression, allowed_names=set(context))
    prepared = {name: value_rules._formula_value(value) for name, value in context.items()}
    result = value_rules._eval_node(parsed.body, prepared)
    if len(str(result)) > value_rules.MAX_RULE_TEXT_LENGTH:
        raise ValueError("formula result is too long")
    return result


def _outcome(calculate):
    try:
        result = calculate()
        return (type(result), str(result))
    except (ArithmeticError, TypeError, ValueError) as error:
        return (type(error), str(error))


class ArithmeticFormulaTests(unittest.TestCase):
    def test_compiled_arithmetic_matches_exact_values_and_errors(self):
        expressions = (
            "value / series * 1000", "-(value + series) / 3", "+value",
            "value - series * 0.1", "(value + series) * (value - series)",
            "1 / 3", "value + series", "value * value + series",
        )
        pairs = (
            ("2.9500", "1"), (1, 3), (Decimal("-0.000"), "2.00"),
            (None, 1), (True, 1), ("", 1), ("word", "other"),
            ("1e3", "1"), ("1,000", "1"), (2.95, 0), (0, 0),
            ("9999999999999999999999999999.99", "0.001"),
            (Decimal("1E+999999"), Decimal("1E+999999")),
            (Decimal("NaN"), 1), (float("inf"), 1),
        )
        for expression in expressions:
            program = compile_arithmetic_formula(expression, allowed_names={"value", "series"})
            self.assertIsNotNone(program)
            for value, series in pairs:
                with self.subTest(expression=expression, value=value, series=series):
                    context = {"value": value, "series": series, "unused": "unrelated"}
                    self.assertEqual(
                        _outcome(lambda: program.evaluate(context)),
                        _outcome(lambda: _oracle(expression, context)),
                    )

    def test_random_decimals_and_context_rounding_match_the_oracle(self):
        generator = random.Random(20260915)
        expression = "(value / series * 1000 + 0.125) * -value"
        program = compile_arithmetic_formula(expression, allowed_names={"value", "series"})
        for precision in (7, 28):
            with localcontext() as decimal_context:
                decimal_context.prec = precision
                decimal_context.rounding = ROUND_DOWN
                for _ in range(100):
                    context = {
                        "value": Decimal(generator.randrange(-10**20, 10**20)).scaleb(-7),
                        "series": Decimal(generator.randrange(1, 1000)).scaleb(-3),
                    }
                    self.assertEqual(str(program.evaluate(context)), str(_oracle(expression, context)))

    def test_only_referenced_source_values_are_converted_once(self):
        program = compile_arithmetic_formula("value * value + series", allowed_names={"series"})
        context = {"value": "2.500", "series": "1", **{f"column_{i}": "100" for i in range(60)}}
        with patch.object(value_rules, "_formula_value", wraps=value_rules._formula_value) as convert:
            self.assertEqual(program.evaluate(context), Decimal("7.250000"))
        named_inputs = [call.args[0] for call in convert.call_args_list if isinstance(call.args[0], str)]
        self.assertEqual(named_inputs, ["1", "2.500"])

    def test_compilation_is_reused_and_row_execution_does_not_walk_ast(self):
        first = compile_arithmetic_formula("column_2 / value", allowed_names={"column_2"})
        second = compile_arithmetic_formula("column_2 / value", allowed_names={"column_2"})
        self.assertIs(first, second)
        with patch.object(value_rules, "_eval_node", side_effect=AssertionError("AST row walk")):
            self.assertEqual(first.evaluate({"column_2": "3", "value": "2"}), Decimal("1.5"))
            self.assertEqual(evaluate_formula("column_2 / value", {"column_2": "3", "value": "2"}), Decimal("1.5"))

    def test_unqualified_expressions_keep_the_authoritative_evaluator(self):
        for expression in ("value % 3", "abs(value)", "1 if value else 0", "value > 3", "True + value"):
            with self.subTest(expression=expression):
                self.assertIsNone(compile_arithmetic_formula(expression, allowed_names=set()))
                context = {"value": 7}
                self.assertEqual(_outcome(lambda: evaluate_formula(expression, context)), _outcome(lambda: _oracle(expression, context)))

    def test_invalid_expressions_retain_validation_reason_and_position(self):
        for expression in ("unknown + value", "value ** 2", "value.__class__", "value +"):
            with self.subTest(expression=expression):
                with self.assertRaises(FormulaValidationError) as old:
                    validate_formula(expression)
                with self.assertRaises(FormulaValidationError) as compiled:
                    compile_arithmetic_formula(expression, allowed_names=set())
                self.assertEqual((compiled.exception.reason, compiled.exception.position), (old.exception.reason, old.exception.position))


if __name__ == "__main__":
    unittest.main()
