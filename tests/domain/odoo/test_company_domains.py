"""Keep the company-domain interpreter closed and independent of business models."""

import unittest

from impodo.domain.odoo.company_domains import (
    hierarchy_parent_domain,
    linked_value_domain,
    operation_company_domain,
)
from tests.support.hierarchy_transport import odoo_hierarchy_domain


class CompanyDomainTests(unittest.TestCase):
    def test_odoo_generated_conditional_company_rule_keeps_both_restrictions(self):
        for company_field in ("company_id", "x_legal_entity"):
            expression = odoo_hierarchy_domain(company_field, "[('kind', '=', 'folder')]")
            with self.subTest(company_field=company_field):
                self.assertEqual(hierarchy_parent_domain(expression, 7, company_field), [
                    [company_field, "in", [7, False]], ["kind", "=", "folder"],
                    "|", [company_field, "=", False], [company_field, "=", 7],
                ])
                self.assertIsNone(linked_value_domain(expression, 7))
                for invalid in (
                    f"{company_field} and [('name', '=', 'ok')] or unknown",
                    f"{company_field} and [('name', '=', 'ok')] or dangerous()",
                    f"{company_field} and [('name', '=', 'ok')] or {company_field}.id",
                    f"{company_field} and [('name', '=', 'ok')] or {{'name': 'bad'}}",
                ):
                    self.assertIsNone(hierarchy_parent_domain(invalid, 7, company_field))

    def test_hierarchy_ownership_does_not_require_a_company_dependent_parent(self):
        for company_field in ("company_id", "x_legal_entity"):
            for company in (7, 105):
                with self.subTest(company_field=company_field, company=company):
                    scope = ["|", [company_field, "=", False], [company_field, "=", company]]
                    self.assertEqual(hierarchy_parent_domain(None, company, company_field), scope)
                    expression = f"[('{company_field}', 'in', [False, {company_field}])] + [('kind', '=', 'folder')]"
                    self.assertEqual(hierarchy_parent_domain(expression, company, company_field), [
                        [company_field, "in", [False, company]], ["kind", "=", "folder"], *scope,
                    ])
                    self.assertIsNone(linked_value_domain(expression, company))
                    self.assertEqual(hierarchy_parent_domain(
                        f"[('{company_field}', 'in', allowed_company_ids)]", company, company_field,
                    ), [[company_field, "in", [company]], *scope])
                    for invalid in ("[('name', '=', other)]", "__import__('os')", "[('name', '=', company_id.id)]"):
                        self.assertIsNone(hierarchy_parent_domain(invalid, company, company_field))

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
