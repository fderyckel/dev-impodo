"""Reference formula text fidelity and shared formula safety contracts."""

from decimal import Decimal
from unittest import TestCase

from impodo.domain.recipe.value_rules import (
    MAX_RULE_TEXT_LENGTH,
    FormulaValidationError,
    evaluate_formula,
    evaluate_reference_formula,
    formula_input_names,
)


class ReferenceFormulaTests(TestCase):
    def test_source_codes_keep_leading_zeroes_with_concat_and_addition(self):
        context = {"value": "0WIL", "column_5": "0100", "column_6": "100"}
        for expression in (
            'concat(value, "/Stock/", column_5, "/", column_6)',
            'value + "/Stock/" + column_5 + "/" + column_6',
        ):
            with self.subTest(expression=expression):
                self.assertEqual(
                    evaluate_reference_formula(expression, context),
                    "0WIL/Stock/0100/100",
                )

    def test_reference_rules_are_not_tied_to_locations(self):
        self.assertEqual(
            evaluate_reference_formula(
                'concat(upper(strip(column_1)), "-", substring(value, 1, 4))',
                {"column_1": " customer ", "value": "A0007B"},
            ),
            "CUSTOMER-0007",
        )

    def test_literals_keep_zeroes_and_numeric_operations_are_explicit(self):
        self.assertEqual(
            evaluate_reference_formula(
                'concat("000", value, "/", column_1 * 2)',
                {"value": "001", "column_1": "03"},
            ),
            "000001/6",
        )

    def test_empty_and_fallback_values_remain_caller_decisions(self):
        self.assertEqual(
            evaluate_reference_formula("concat(value)", {"value": None}), ""
        )
        self.assertEqual(
            evaluate_reference_formula('coalesce(value, "000")', {"value": None}),
            "000",
        )
        self.assertEqual(
            evaluate_reference_formula(
                'value if value != "" else "001"', {"value": ""}
            ),
            "001",
        )

    def test_typed_source_values_retain_their_original_text_representation(self):
        self.assertEqual(
            evaluate_reference_formula(
                'concat(value, "/", column_1)',
                {"value": 1e-5, "column_1": Decimal("0.00100")},
            ),
            "1e-05/0.00100",
        )
        self.assertEqual(
            evaluate_reference_formula("concat(value * 2)", {"value": 1e-5}),
            "0.00002",
        )

    def test_result_requires_text_and_uses_existing_size_bound(self):
        for expression in ("value + 1", "True", "None"):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(TypeError, "must produce text"):
                    evaluate_reference_formula(expression, {"value": 1})
        with self.assertRaisesRegex(ValueError, "result is too long"):
            evaluate_reference_formula(
                "concat(value, value)", {"value": "A" * (MAX_RULE_TEXT_LENGTH // 2 + 1)}
            )

    def test_reference_formulas_reuse_the_same_safety_validation(self):
        for expression in (
            "value.__class__",
            "__import__('os')",
            "[value for item in value]",
            "value[0]",
            "open(value)",
            "concat(value, missing)",
            "concat(value, sep='/')",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(FormulaValidationError):
                    evaluate_reference_formula(expression, {"value": "001"})

    def test_existing_scalar_numeric_behavior_is_unchanged(self):
        context = {"value": "0100", "column_1": "002"}
        self.assertEqual(evaluate_formula("value + column_1", context), Decimal(102))
        self.assertEqual(
            evaluate_formula('concat(value, "-", column_1)', context), "100-2"
        )

    def test_input_names_exclude_helpers_and_deduplicate_source_bindings(self):
        self.assertEqual(
            formula_input_names(
                "concat(strip(value), column_2, value, upper(column_1))",
                allowed_names={"column_1", "column_2"},
            ),
            ("column_1", "column_2", "value"),
        )
        self.assertEqual(formula_input_names('concat("001", "002")'), ())
        with self.assertRaises(FormulaValidationError):
            formula_input_names("column_3", allowed_names={"column_1"})
