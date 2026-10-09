from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from tests.application.workspace.test_transfer_order import _model
from tests.support.paths import REPOSITORY_ROOT


class TransferReviewPresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                    FileSystemLoader(
                        REPOSITORY_ROOT / "src/impodo/web/templates"
                    ),
                ]
            ),
            autoescape=True,
        )

    def test_reference_only_model_is_locked_and_names_ignored_records(self) -> None:
        company = replace(
            _model("res.company", "Company", existing=1),
            destination_handling="reference_only",
            unreferenced_source_row_numbers=(2, 3),
        )

        html = self.templates.get_template(
            "workspace_transfer_review.html"
        ).render(
            migration_project=SimpleNamespace(display_name="Test project"),
            transfer_review_approved=False,
            transfer_review_current=False,
            transfer_review_stale=False,
            transfer_review_package=None,
            transfer_match_models=(company,),
            workspace_state=SimpleNamespace(revision=7),
            workspace_id="workspace-1",
            csrf_token="csrf",
        )

        self.assertIn("Review what happens for each record type", html)
        self.assertIn("Use existing destination records only", html)
        self.assertIn(
            f'name="policy_{company.dataset_id}" value="reuse_only"',
            html,
        )
        self.assertNotIn(f'<select id="policy_{company.dataset_id}"', html)
        self.assertIn("<strong>Company</strong>", html)
        self.assertNotIn("Company (res.company)", html)
        self.assertIn("Odoo model <code>res.company</code>", html)
        self.assertIn("this record type will not be created or updated", html)
        self.assertIn("2 unrelated captured record(s) ignored", html)
        self.assertNotIn("Update existing, create missing", html)

    def test_preserve_linked_model_has_fixed_create_if_missing_policy(self) -> None:
        uom = replace(
            _model("uom.uom", "Unit of Measure", existing=1),
            destination_handling="reuse_or_create",
        )

        html = self.templates.get_template(
            "workspace_transfer_review.html"
        ).render(
            migration_project=SimpleNamespace(display_name="Test project"),
            transfer_review_approved=False,
            transfer_review_current=False,
            transfer_review_stale=False,
            transfer_review_package=None,
            transfer_match_models=(uom,),
            workspace_state=SimpleNamespace(revision=7),
            workspace_id="workspace-1",
            csrf_token="csrf",
        )

        self.assertIn(
            f'name="policy_{uom.dataset_id}" value="create_if_missing"',
            html,
        )
        self.assertNotIn(f'<select id="policy_{uom.dataset_id}"', html)
        self.assertIn(
            '<p class="hint" role="status"><strong>Keep linked value</strong></p>',
            html,
        )
        self.assertIn("Fixed by the Stage 2 related-data decision", html)
        self.assertIn(
            "A matching destination record is reused unchanged",
            html,
        )
        self.assertNotIn("Update existing, create missing", html)
        self.assertIn("<strong>Unit of Measure</strong>", html)
        self.assertNotIn("Unit of Measure (uom.uom)", html)
        self.assertIn("<summary>Support details</summary>", html)
        self.assertIn("Odoo model <code>uom.uom</code>", html)

    def test_transferred_model_describes_selected_field_writes(self) -> None:
        product = _model("product.template", "Product", create=1)

        html = self.templates.get_template(
            "workspace_transfer_review.html"
        ).render(
            migration_project=SimpleNamespace(display_name="Test project"),
            transfer_review_approved=False,
            transfer_review_current=False,
            transfer_review_stale=False,
            transfer_review_package=None,
            transfer_match_models=(product,),
            workspace_state=SimpleNamespace(revision=7),
            workspace_id="workspace-1",
            csrf_token="csrf",
        )

        self.assertIn(
            "Reuse existing; create missing records using selected fields",
            html,
        )
        self.assertIn(
            "Update existing and create missing records using selected fields",
            html,
        )
        self.assertNotIn("create full missing records", html)


if __name__ == "__main__":
    unittest.main()
