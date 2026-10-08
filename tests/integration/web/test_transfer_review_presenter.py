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

        self.assertIn("Use only to match related destination records", html)
        self.assertIn(
            f'name="policy_{company.dataset_id}" value="reuse_only"',
            html,
        )
        self.assertIn("this record type will not be created or updated", html)
        self.assertIn("2 unrelated captured record(s) ignored", html)
        self.assertNotIn("Update existing, create missing", html)


if __name__ == "__main__":
    unittest.main()
