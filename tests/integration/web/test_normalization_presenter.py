from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import Request
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.domain.preparation.quality import (
    QualityDisposition,
    QualityIssue,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityReviewItem,
    QualityReviewPage,
    QualityRowResult,
    QualityRuleFamily,
)
from impodo.web.presenters.summary import _render_normalization
from tests.support.paths import REPOSITORY_ROOT


class NormalizationSetAsideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.summary = SimpleNamespace(
            run_id="prepared-run",
            quality_run_id="quality-bound-to-prepared-run",
            content_hash="prepared-hash",
            eligible_dataset_hash="eligible-hash",
            eligible_record_count=10,
            changed_record_count=0,
            set_aside_record_count=2,
            lifecycle_version=1,
            decisions_left=0,
            frozen=False,
        )
        self.context = MagicMock()
        self.context.normalization.current_group_review.return_value = (
            self.summary,
            (),  # Set-aside records need not have normalization changes.
            SimpleNamespace(
                group_decisions=(), status=SimpleNamespace(value="REVIEW_REQUIRED"),
            ),
            0,
        )
        self.templates = Environment(
            loader=ChoiceLoader([
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            ]),
            autoescape=True,
        )

    def _item(
        self,
        *,
        disposition: QualityDisposition = QualityDisposition.QUARANTINED,
        with_issues: bool = True,
    ) -> QualityReviewItem:
        row_id = "sha256:" + "1" * 64
        issues = tuple(
            QualityIssue(
                issue_id="sha256:" + digit * 64,
                rule_id="sha256:" + "4" * 64,
                family=QualityRuleFamily.BOUNDED_VALUES,
                reason_code=code,
                message=message,
                dataset="contacts",
                row_id=row_id,
                source_row=37,
                affected_fields=("amount",),
                policy=policy,
                owner_role=QualityOwnerRole.DATA_MANAGER,
                owner_label="Data manager",
            )
            for digit, code, message, policy in (
                ("2", "SOURCE_FORMULA_INVALID", "Formula requires numbers.", QualityOutcomePolicy.QUARANTINE),
                ("3", "POST_TRANSFORM_IDENTITY_COLLISION", "Two records use the same Odoo match.", QualityOutcomePolicy.QUARANTINE),
                ("5", "BUSINESS_CHECK_FAILED", "Check the contact channel.", QualityOutcomePolicy.WARNING),
            )
        ) if with_issues else ()
        return QualityReviewItem(
            row=QualityRowResult(
                row_id=row_id,
                dataset="contacts",
                source_row=37,
                record_label="Customer <script>unsafe</script>",
                base_disposition=QualityDisposition.CANDIDATE,
                effective_disposition=disposition,
                issue_ids=tuple(sorted(issue.issue_id for issue in issues)),
                requires_review=with_issues,
            ),
            issues=issues,
            correction_route="Correct the source value and prepare again." if issues else "",
        )

    def _render(self, query: str = "status=set_aside") -> tuple[str, dict]:
        request = Request({"type": "http", "query_string": query.encode()})
        with patch("impodo.web.presenters.summary._render") as render:
            _render_normalization(request, self.context, "workspace")
        values = render.call_args.kwargs
        html = self.templates.get_template("workspace_normalization.html").render(
            **values,
            workspace_id="workspace",
            migration_context=SimpleNamespace(project_id="project"),
            workspace_navigation=SimpleNamespace(journey="AUTHORING"),
        )
        return html, values

    def test_set_aside_records_and_all_findings_render_without_change_groups(self) -> None:
        self.context.queries.get_quality_review_page.return_value = QualityReviewPage(
            items=(self._item(),), matching_count=1, page=1, page_count=1,
        )

        html, values = self._render()

        self.context.queries.get_quality_review_page.assert_called_once_with(
            "workspace", "quality-bound-to-prepared-run",
            status="quarantined", dataset="", page=1, page_size=50,
        )
        self.assertEqual(values["review_items"], ())
        self.assertIn("data-normalization-set-aside-row", html)
        self.assertIn("Records 1-1 of 1", html)
        self.assertIn("<td>37</td>", html)
        self.assertIn("Customer &lt;script&gt;unsafe&lt;/script&gt;", html)
        self.assertNotIn("<script>unsafe</script>", html)
        self.assertIn("Formula requires numbers.", html)
        self.assertIn("Two records use the same Odoo match.", html)
        self.assertIn("Fields: amount", html)
        self.assertIn("this warning did not exclude the record", html)
        self.assertIn("Correct the source value and prepare again.", html)
        self.assertNotIn("No changes in this view", html)

    def test_pagination_uses_record_count_and_retains_filter(self) -> None:
        self.context.queries.get_quality_review_page.return_value = QualityReviewPage(
            items=(self._item(),), matching_count=101, page=2, page_count=3,
        )

        html, values = self._render("status=set_aside&page=2")

        self.assertEqual(values["review_matching_count"], 101)
        self.assertEqual(values["review_page_count"], 3)
        self.assertIn("Records 51-100 of 101", html)
        self.assertIn("?status=set_aside&amp;page=1#review-groups", html)
        self.assertIn("?status=set_aside&amp;page=3#review-groups", html)

    def test_page_is_clamped_by_the_quality_query(self) -> None:
        self.context.queries.get_quality_review_page.return_value = QualityReviewPage(
            items=(self._item(),), matching_count=51, page=2, page_count=2,
        )

        html, values = self._render("status=set_aside&page=999")

        self.assertEqual(values["review_page"], 2)
        self.assertIsNone(values["review_next_url"])
        self.assertIn("Records 51-51 of 51", html)

    def test_exclusions_without_findings_have_an_explanation_after_approval(self) -> None:
        self.summary.frozen = True
        self.context.queries.get_quality_review_page.return_value = QualityReviewPage(
            items=(self._item(disposition=QualityDisposition.EXCLUDED, with_issues=False),),
            matching_count=1, page=1, page_count=1,
        )

        html, _ = self._render()

        self.assertIn("Excluded during preparation by the row-selection rules.", html)
        self.assertIn("Not included", html)
        self.assertNotIn("Included in approval", html)

    def test_empty_set_aside_view_explains_that_no_records_were_set_aside(self) -> None:
        self.summary.set_aside_record_count = 0
        self.context.queries.get_quality_review_page.return_value = QualityReviewPage(
            items=(), matching_count=0, page=1, page_count=1,
        )

        html, _ = self._render("status=set_aside&page=invalid")

        self.assertIn("No set-aside records", html)
        self.assertNotIn("No changes in this view", html)
        self.assertNotIn("Records 1-0", html)
        self.assertEqual(self.context.queries.get_quality_review_page.call_args.kwargs["page"], 1)

    def test_other_filters_keep_the_change_group_view(self) -> None:
        html, values = self._render("status=automatic")

        self.context.queries.get_quality_review_page.assert_not_called()
        self.assertIsNone(values["set_aside_page"])
        self.assertIn("Routine preparation", html)
        self.assertIn("No changes in this view", html)


if __name__ == "__main__":
    unittest.main()
