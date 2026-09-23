from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.domain.preflight.deferred_scope import (
    DeferredIssue,
    DeferredIssueGroup,
    DeferredIssueScope,
    DeferredOmittedRow,
    DeferredScopeDecision,
    DeferredScopePreview,
)
from impodo.domain.shared.access import ActorIdentity
from tests.support.paths import REPOSITORY_ROOT


class DeferredScopePresenterTests(unittest.TestCase):
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
        self.issue_id = "sha256:" + "1" * 64
        self.row_id = "sha256:" + "2" * 64
        self.preview = DeferredScopePreview(
            comparison_id="comparison-1",
            comparison_hash="sha256:" + "3" * 64,
            execution_snapshot_hash="sha256:" + "4" * 64,
            selected_issue_ids=(self.issue_id,),
            groups=(
                DeferredIssueGroup(
                    group_id="sha256:" + "5" * 64,
                    issue_id=self.issue_id,
                    root_row_id=self.row_id,
                    row_ids=(self.row_id,),
                    counts_by_dataset=(("bom_lines", 1),),
                    write_count=0,
                ),
            ),
            omitted_rows=(
                DeferredOmittedRow(
                    row_id=self.row_id,
                    dataset="bom_lines",
                    source_row=17,
                    source_trace_id="trace-1",
                    disposition="BLOCKED",
                    direct_issue_ids=(self.issue_id,),
                    inherited_issue_ids=(),
                ),
            ),
            counts_by_dataset=(("bom_lines", 1),),
            prepared_record_count=4,
            already_set_aside_count=1,
            original_write_count=2,
            omitted_write_count=0,
            remaining_write_count=2,
            remaining_problem_record_count=0,
            remaining_run_issue_count=0,
        )
        self.issue = DeferredIssue(
            issue_id=self.issue_id,
            code="REFERENCE_NOT_FOUND",
            scope=DeferredIssueScope.ROW,
            row_id=self.row_id,
            field="product_id",
            message="Missing <script>unsafe</script> component",
        )

    def _render(self, *, decision=None) -> str:
        review = SimpleNamespace(
            comparison_id=self.preview.comparison_id,
            issues=(self.issue,),
            candidates=(
                SimpleNamespace(
                    issue=self.issue,
                    selectable=True,
                    dataset="bom_lines",
                    source_row=17,
                    source_identity=("BOM-1", "LINE-1"),
                ),
            ),
            preview=self.preview,
            decision=decision,
        )
        return self.templates.get_template("workspace_deferred_scope.html").render(
            deferred_review=review,
            error=None,
            csrf_token="csrf",
            workspace_id="workspace-1",
            migration_project=SimpleNamespace(display_name="Test project"),
        )

    def test_safe_preview_shows_exact_impact_and_explicit_acceptance(self) -> None:
        html = self._render()

        self.assertIn("Preview affected groups", html)
        self.assertIn("Set aside affected groups for this load", html)
        self.assertIn("<strong>4</strong>", html)
        self.assertIn("<strong>2</strong>", html)
        self.assertIn("source row 17", html)
        self.assertIn("Direct problem", html)
        self.assertIn("Missing &lt;script&gt;unsafe&lt;/script&gt; component", html)
        self.assertNotIn("<script>unsafe</script>", html)

    def test_accepted_preview_is_read_only_and_names_reviewer(self) -> None:
        decision = DeferredScopeDecision.accept(
            workspace_id="workspace-1",
            preview=self.preview,
            reduced_execution_snapshot_hash="sha256:" + "6" * 64,
            accepted_by=ActorIdentity("test", "manager", "Data manager"),
            accepted_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        )

        html = self._render(decision=decision)

        self.assertIn("Ready with records set aside", html)
        self.assertIn("Accepted by Data manager", html)
        self.assertNotIn("Set aside affected groups for this load</button>", html)


if __name__ == "__main__":
    unittest.main()
