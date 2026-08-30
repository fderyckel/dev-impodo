import unittest

from impodo.application.source_workspace_service import (
    _dataset_name_violations,
)


class DatasetNameValidationTests(unittest.TestCase):
    def test_valid_name_has_no_violations(self) -> None:
        self.assertEqual(_dataset_name_violations("product_with_uom_v1"), ())

    def test_blank_name_explains_that_a_name_is_required(self) -> None:
        self.assertEqual(
            _dataset_name_violations(""),
            ("Enter a name.",),
        )

    def test_long_name_explains_the_length_limit(self) -> None:
        self.assertEqual(
            _dataset_name_violations("a" * 64),
            ("Use no more than 63 characters.",),
        )

    def test_uppercase_name_explains_each_broken_rule(self) -> None:
        self.assertEqual(
            _dataset_name_violations("Product_withUoM_v1"),
            (
                "Start with a lowercase letter from a to z.",
                "Use only lowercase letters, numbers, and underscores.",
            ),
        )

    def test_punctuation_explains_the_allowed_characters(self) -> None:
        self.assertEqual(
            _dataset_name_violations("product-with-uom"),
            ("Use only lowercase letters, numbers, and underscores.",),
        )


if __name__ == "__main__":
    unittest.main()
