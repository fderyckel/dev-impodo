from __future__ import annotations

import unittest
from uuid import uuid4

from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueSeverity,
)
from impodo.application.workspace.navigation import WorkspaceNavigationFacts
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)
from impodo.web.presenters.matching_health_issues import (
    matching_health_workflow_issue_summary,
)
from impodo.web.presenters.navigation import build_workspace_navigation
from tests.application.workspace.mapping.test_matching_health_issues import (
    _blocked_identity,
    _check,
    _unchecked_relationship,
)


class MatchingHealthIssuePresenterTests(unittest.TestCase):
    def test_projects_current_identity_and_inverse_relationship_actions(self) -> None:
        check = _check(
            identity_results=(_blocked_identity(),),
            relationship_health_results=(_unchecked_relationship(),),
        )

        summary = matching_health_workflow_issue_summary(
            check=check,
            check_current=True,
            identity_health={
                "cards": (
                    {
                        "dataset_id": "products",
                        "dataset_name": "Products",
                        "source_blank_row_count": 1,
                        "source_repeated_row_count": 0,
                        "target_ambiguous_key_count": 0,
                        "blocked_count": 1,
                        "target_fields": ("default_code",),
                        "not_checkable_reason": "",
                        "edit_url": "/workspaces/workspace:test/mapping#identity",
                    },
                )
            },
            relationship_health={
                "cards": (
                    {
                        "dataset_id": "lines",
                        "dataset_name": "Lines",
                        "technical_field": "line_ids",
                        "field_label": "Lines",
                        "resolver_origin": "TARGET_CATALOG",
                        "not_checkable_reason": "ONE2MANY_INVERSE_REQUIRED",
                        "blocked_count": 0,
                        "edit_url": "/workspaces/workspace:test/mapping#relationship",
                        "inverse_action": {
                            "label": "Open incoming Lines -> parent_id",
                            "url": "/workspaces/workspace:test/mapping#parent_id",
                        },
                    },
                )
            },
        )

        self.assertEqual(summary.must_fix_count, 1)
        self.assertEqual(summary.review_count, 1)
        identity, relationship = summary.issues
        self.assertIs(identity.severity, WorkflowIssueSeverity.MUST_FIX)
        self.assertIs(identity.origin, WorkflowIssueOrigin.SOURCE_DATA)
        self.assertEqual(identity.affected_record_count, 1)
        self.assertEqual(
            identity.correction_route,
            "/workspaces/workspace:test/mapping#identity",
        )
        self.assertIs(relationship.severity, WorkflowIssueSeverity.REVIEW)
        self.assertEqual(
            relationship.correction_route,
            "/workspaces/workspace:test/mapping#parent_id",
        )

    def test_stale_check_does_not_project_findings(self) -> None:
        summary = matching_health_workflow_issue_summary(
            check=_check(identity_results=(_blocked_identity(),)),
            check_current=False,
            identity_health={"cards": ()},
            relationship_health=None,
        )

        self.assertEqual(summary.issues, ())

    def test_navigation_keeps_current_stage_three_counts_visible(self) -> None:
        workspace = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Products",
            source_system="CSV",
            source_mode=SourceMode.FILE,
            status=WorkspaceStatus.REGISTERED,
        )
        navigation = build_workspace_navigation(
            WorkspaceNavigationFacts(
                workspace_id=workspace.workspace_id,
                source_selection_hash="sha256:" + "1" * 64,
                schema_present=True,
                governance_present=True,
                workflow_issue_must_fix_count=2,
                workflow_issue_review_count=1,
            ),
            workspace,
            "mapping/page.html",
        )

        self.assertEqual(navigation.workflow_issue_must_fix_count, 2)
        self.assertEqual(navigation.workflow_issue_review_count, 1)
        self.assertEqual(
            navigation.workflow_issue_href,
            f"/workspaces/{workspace.workspace_id}/mapping#migration-issues",
        )


if __name__ == "__main__":
    unittest.main()
