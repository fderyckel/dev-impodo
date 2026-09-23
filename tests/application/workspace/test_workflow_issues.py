from __future__ import annotations

import unittest

from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueProjection,
    WorkflowIssueSeverity,
    WorkflowIssueState,
    summarize_workflow_issues,
)


class WorkflowIssueProjectionTests(unittest.TestCase):
    def test_summary_keeps_current_findings_and_sorts_blockers_first(self) -> None:
        summary = summarize_workflow_issues(
            (
                _issue("review", WorkflowIssueSeverity.REVIEW),
                _issue("resolved", WorkflowIssueSeverity.MUST_FIX, resolved=True),
                _issue("blocker", WorkflowIssueSeverity.MUST_FIX),
            )
        )

        self.assertEqual(
            tuple(issue.code for issue in summary.issues),
            ("blocker", "review"),
        )
        self.assertEqual(summary.must_fix_count, 1)
        self.assertEqual(summary.review_count, 1)


def _issue(
    code: str,
    severity: WorkflowIssueSeverity,
    *,
    resolved: bool = False,
) -> WorkflowIssueProjection:
    return WorkflowIssueProjection(
        code=code,
        severity=severity,
        state=(
            WorkflowIssueState.RESOLVED
            if resolved
            else WorkflowIssueState.CURRENT
        ),
        cause=f"Cause {code}",
        origin=WorkflowIssueOrigin.ODOO_SCHEMA,
        owner="Data manager",
        owning_stage="Odoo data",
        correction_label="Review Odoo data",
        correction_route="/workspaces/example/schema",
        preserved_work="Accepted source data remains unchanged.",
        recheck="Check Odoo data again.",
        evidence_revision="sha256:" + "a" * 64,
    )


if __name__ == "__main__":
    unittest.main()
