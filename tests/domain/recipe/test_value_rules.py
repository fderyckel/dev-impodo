"""Safe-formula parser and browser-authoring projections."""

from __future__ import annotations

import unittest

from impodo.domain.recipe.value_rules import (
    FormulaValidationError,
    validate_formula,
)
from impodo.web.mapping_formula_authoring import validate_formula_authoring


class FormulaValidationTests(unittest.TestCase):
    def test_parser_accepts_documented_values_and_helpers(self) -> None:
        for expression in (
            "value == 10",
            'value != "UNI"',
            "coalesce(value, column_2) == column_1",
        ):
            with self.subTest(expression=expression):
                parsed = validate_formula(
                    expression,
                    allowed_names={"column_1", "column_2"},
                )

                self.assertIsNotNone(parsed)

    def test_syntax_error_has_a_stable_reason_and_character(self) -> None:
        expression = 'value 1= "UNI"'

        with self.assertRaises(FormulaValidationError) as captured:
            validate_formula(expression)

        self.assertIsInstance(captured.exception, ValueError)
        self.assertEqual(captured.exception.reason, "FORMULA_SYNTAX_INVALID")
        self.assertIsNotNone(captured.exception.position)

    def test_unknown_value_points_to_its_start(self) -> None:
        with self.assertRaises(FormulaValidationError) as captured:
            validate_formula("column_9 + value", allowed_names={"column_1"})

        self.assertEqual(captured.exception.reason, "FORMULA_UNKNOWN_VALUE")
        self.assertEqual(captured.exception.position, 1)

    def test_authoring_issue_does_not_echo_the_formula(self) -> None:
        expression = "confidential_source_name + 1"

        issue = validate_formula_authoring(expression, allowed_names=set())

        self.assertIsNotNone(issue)
        payload = issue.portable_dict()
        self.assertNotIn(expression, repr(payload))
        self.assertNotIn("confidential_source_name", repr(payload))
        self.assertEqual(payload["code"], "MAPPING_FORMULA_INVALID")
        self.assertIn("available column_N", payload["correction"])


if __name__ == "__main__":
    unittest.main()
