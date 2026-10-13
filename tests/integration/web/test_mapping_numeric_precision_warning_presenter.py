from __future__ import annotations

from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader

from tests.support.paths import REPOSITORY_ROOT


class MappingNumericPrecisionWarningPresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )

    def test_stage_three_warning_names_later_blocker_and_direct_fix(self) -> None:
        issue = SimpleNamespace(
            code="MAPPING_TARGET_NUMERIC_PRECISION_LOSS",
            message=(
                "mrp.bom.line.product_qty: 4,978 mapped value(s) cannot be "
                "stored exactly at Odoo precision (16, 2)."
            ),
            remediation=(
                "Open Decimal rounding and select Round to this Odoo precision."
            ),
            target_model="mrp.bom.line",
            target_field="product_qty",
        )
        html = self.templates.get_template(
            "mapping/_numeric_precision_warnings.html"
        ).render(
            warning_issues=(
                {
                    "issue": issue,
                    "dataset_label": "BOM Lines",
                    "field_label": "Product Quantity",
                    "fix_url": "/workspaces/workspace-1/mapping?field_query=Product",
                },
            ),
        )

        self.assertIn("Potential Load into Odoo blocker", html)
        self.assertIn("BOM Lines: Product Quantity needs a number decision", html)
        self.assertIn("4,978 mapped value(s)", html)
        self.assertIn("Round to this Odoo precision", html)
        self.assertIn("Adjust this number rule", html)
        self.assertIn("MAPPING_TARGET_NUMERIC_PRECISION_LOSS", html)
        self.assertIn("mrp.bom.line.product_qty", html)

    def test_unrelated_mapping_warning_is_not_rendered_twice(self) -> None:
        html = self.templates.get_template(
            "mapping/_numeric_precision_warnings.html"
        ).render(
            warning_issues=(
                {
                    "issue": SimpleNamespace(
                        code="MAPPING_OTHER_WARNING",
                        message="Other warning",
                    ),
                },
            ),
        )

        self.assertNotIn("Potential Load into Odoo blocker", html)
        self.assertNotIn("Other warning", html)


if __name__ == "__main__":
    unittest.main()
