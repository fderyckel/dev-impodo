"""Detect missing parents in Match data and repair explicit zero roots in place."""

from impodo.domain.mapping.contracts import IdentityNullPolicy
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.same_table_hierarchy import CATEGORY_CSV, same_table_workspace


class ParentRootFeedbackTests(HierarchyWorkflowFixture):
    def test_zero_marker_is_explicit_and_repair_preserves_source_for_both_majors_and_modes(
        self,
    ):
        content = CATEGORY_CSV.replace(
            b"5637144576,Materials,\n", b"5637144576,Materials,0\n"
        )
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id, _ = same_table_workspace(
                        self, major=major, mode=mode, content=content
                    )
                    original = self.context.queries.get_source_selection(workspace_id)
                    revision = self.context.queries.get_mapping_revision(workspace_id)
                    validation = self.submit(
                        workspace_id, revision.definition.datasets, check_only=True
                    )
                    self.assertEqual(validation.status.value, "INVALID")
                    issue = next(
                        issue
                        for issue in validation.issues
                        if issue.code == "MAPPING_INCOMING_PARENT_MISSING"
                    )
                    self.assertEqual(issue.target_field, "parent_id")
                    self.assertIn("0 is not a blank root", issue.message)
                    self.assertIn("0 means no parent", issue.remediation)
                    mapping = revision.definition.datasets[0]
                    from dataclasses import replace

                    corrected = replace(
                        mapping,
                        target_scope=(
                            replace(
                                mapping.target_scope[0],
                                null_policy=IdentityNullPolicy.ZERO_AS_SCOPE_NULL,
                            ),
                        ),
                    )
                    self.submit(workspace_id, (corrected,))
                    staging, quality = self.prepare(workspace_id)
                    self.assertEqual(
                        (quality.ready_count, quality.quarantined_count), (4, 0)
                    )
                    root = next(
                        row
                        for row in staging.rows
                        if row.target_identity == ("Materials",)
                    )
                    self.assertEqual(root.target_scope, (None,))
                    self.assertEqual(
                        original.content_hash,
                        self.context.queries.get_source_selection(
                            workspace_id
                        ).content_hash,
                    )
                    page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertIn(
                        'name="identity_zero_root_0_1" value="1" checked', page.text
                    )

    def test_all_set_aside_opens_causes_and_never_offers_empty_approval(self):
        content = CATEGORY_CSV.replace(b"5637144576,Materials,\n", b"5637144576,,\n")
        workspace_id, _ = same_table_workspace(self, content=content)
        revision = self.context.queries.get_mapping_revision(workspace_id)
        self.submit(workspace_id, revision.definition.datasets)
        _, quality = self.prepare(workspace_id)
        self.assertEqual((quality.ready_count, quality.quarantined_count), (0, 4))
        page = self.client.get(f"/workspaces/{workspace_id}/normalization")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("All records were set aside", page.text)
        self.assertIn("Migration issues", page.text)
        self.assertNotIn("data-normalization-approve-trigger", page.text)
