"""Keep the company-domain interpreter closed and independent of business models."""

import unittest

from impodo.domain.odoo.company_domains import (
    linked_value_domain,
    operation_company_domain,
)


class CompanyDomainTests(unittest.TestCase):
    def test_linked_choices_keep_the_complete_company_and_field_rule(self):
        expression = "[('company_ids', 'parent_of', [allowed_company_ids[0]])] + [('account_type', '=', 'liability_payable')]"
        self.assertEqual(linked_value_domain(expression, 105), [
            ["company_ids", "parent_of", [105]],
            ["account_type", "=", "liability_payable"],
        ])
        self.assertEqual(linked_value_domain("[]", 105), [])
        for invalid in (
            "__import__('os')", "[('company_ids', '=', allowed_company_ids[1])]",
            "[('name', '=', unknown)]", "['|', ('name', '=', 'a')]",
            "[('name.id', '=', 1)]", "[('name', 'any!', [])]",
            "[('name', '=', {'unsafe': 1})]", "[('name', '=', 'a')] * 2000",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(linked_value_domain(invalid, 105))

    def test_standard_shared_and_parent_domains_use_the_selected_company(self):
        self.assertEqual(
            operation_company_domain(
                "[('company_id', 'in', [allowed_company_ids[0]] + [False])] + []", 105
            ),
            [["company_id", "in", [105, False]]],
        )
        self.assertEqual(
            operation_company_domain(
                "['|', ('company_id', '=', False), ('company_id', 'parent_of', [allowed_company_ids[0]])] + []",
                7001,
            ),
            ["|", ["company_id", "=", False], ["company_id", "parent_of", [7001]]],
        )

    def test_calls_attributes_unknown_names_and_noncompany_predicates_are_not_evaluated(
        self,
    ):
        for expression in (
            "__import__('os').system('unused')",
            "allowed_company_ids.__class__",
            "[('company_id', '=', unknown)] + []",
            "[('name', '=', 'Customer')] + []",
            "[('company_id', '=', allowed_company_ids[1])] + []",
            "[" * 9000,
            "(company_id and [('company_id', '=', company_id)] or []) + []",
        ):
            with self.subTest(expression=expression[:60]):
                self.assertIsNone(operation_company_domain(expression, 105))
