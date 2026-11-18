"""Exercise row selection, confirmation and recovery through the Stage 3 browser routes."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from impodo.domain.mapping.row_inclusion_review import (
    RowInclusionDatasetReview,
    RowInclusionReviewIdentity,
    RowInclusionReviewOutcome,
    RowInclusionReviewReport,
    RowInclusionReviewRow,
    RowInclusionSourceValue,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    patch,
)
from tests.support.identity import resolve_local_actor


class MappingRowCheckTests(ProjectSetupBrowserTestCase):
    def test_rows_to_use_are_checked_reviewed_and_confirmed_exactly(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
        )
        context = self.app.state.context
        source_identity, source_value = dataset.columns
        condition_id = str(uuid4())
        operation_id = str(uuid4())
        entries = [
            ["csrf_token", self.csrf],
            ["action", "draft"],
            ["operation_id", operation_id],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["row_inclusion_mode_0", "matching_rows"],
            ["row_inclusion_join_0", "all"],
            ["row_inclusion_condition_id_0_0", condition_id],
            ["row_inclusion_source_0_0", source_value.stable_key],
            ["row_inclusion_operator_0_0", "equals"],
            ["row_inclusion_value_0_0", "Example"],
            ["row_inclusion_type_0_0", "string"],
        ]

        def checked_rows(*args, **_kwargs):
            pending_receipt = context.mapping_workspace.get_mutation_receipt(
                workspace_id,
                operation_id,
                actor=resolve_local_actor(context),
            )
            self.assertIsNotNone(pending_receipt)
            self.assertEqual(pending_receipt.state.value, "PENDING")
            definition = args[1]
            physical_selection = args[2]
            effective_selection = args[3]
            plan = args[4]
            sentence = "Include a row when Value is exactly Example."
            report = RowInclusionReviewReport(
                identity=RowInclusionReviewIdentity(
                    physical_selection_hash=physical_selection.content_hash,
                    source_selection_hash=effective_selection.content_hash,
                    mapping_content_hash=definition.content_hash,
                    schema_hash=definition.schema_hash,
                    derived_plan_hash=(plan.content_hash if plan is not None else None),
                ),
                datasets=(
                    RowInclusionDatasetReview(
                        dataset.dataset_id,
                        dataset.name,
                        2,
                        1,
                        1,
                        0,
                        sentence,
                    ),
                ),
                rows=(
                    RowInclusionReviewRow(
                        dataset.dataset_id,
                        dataset.name,
                        1,
                        (
                            RowInclusionSourceValue(
                                source_value.stable_key,
                                source_value.source_name,
                                "Example",
                            ),
                        ),
                        RowInclusionReviewOutcome.INCLUDED,
                        sentence,
                    ),
                    RowInclusionReviewRow(
                        dataset.dataset_id,
                        dataset.name,
                        2,
                        (
                            RowInclusionSourceValue(
                                source_value.stable_key,
                                source_value.source_name,
                                "Ignore",
                            ),
                        ),
                        RowInclusionReviewOutcome.EXCLUDED,
                        sentence,
                    ),
                ),
            )
            return SimpleNamespace(row_inclusion_review=report)

        with patch(
            "impodo.application.workspace.mapping.row_inclusion_review."
            "stage_browser_mapping",
            side_effect=checked_rows,
        ):
            checked = self.client.post(
                f"/workspaces/{workspace_id}/mapping/save",
                json={"entries": entries},
                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            )

        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "committed")
        self.assertIn("Review and confirm 1 row to use", checked.json()["message"])
        receipt = context.mapping_workspace.get_mutation_receipt(
            workspace_id,
            operation_id,
            actor=resolve_local_actor(context),
        )
        snapshot, _confirmation = context.row_inclusion_reviews.current(
            workspace_id,
            actor=resolve_local_actor(context),
        )
        self.assertIsNotNone(receipt)
        self.assertIsNotNone(snapshot)
        self.assertEqual(receipt.state.value, "COMMITTED")
        self.assertEqual(receipt.content_identity, snapshot.snapshot_hash)
        result_url = (
            f"/workspaces/{workspace_id}/mapping?step=confirm#rows-to-use-review"
        )
        self.assertEqual(checked.json()["redirect_url"], result_url)
        recovered = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}"
        )
        self.assertEqual(recovered.json()["redirect_url"], result_url, recovered.text)
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Rows to use", page.text)
        self.assertIn("1 included, 1 excluded", page.text)
        self.assertIn("Confirm 1 row to use", page.text)

        review = self.client.get(
            f"/workspaces/{workspace_id}/mapping/rows-to-use?outcome=excluded"
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertIn("Ignore", review.text)
        self.assertIn("Excluded by rule", review.text)

        revision = context.queries.get_mapping_revision(workspace_id)
        working = context.queries.get_mapping_working_draft(workspace_id)
        self.assertIsNotNone(revision)
        self.assertIsNotNone(working)
        current_values = {
            "action": "submit",
            "operation_id": str(uuid4()),
            "expected_parent_version": str(revision.version),
            "expected_working_draft_version": str(working.version),
        }
        current_entries = [
            [name, current_values.get(name, value)] for name, value in entries
        ]
        blocked = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": current_entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertIn("Check and confirm", blocked.json()["detail"])

        confirm_entries = [
            ["csrf_token", self.csrf],
            ["action", "confirm_rows"],
            ["expected_parent_version", str(revision.version)],
            ["expected_working_draft_version", str(working.version)],
        ]
        confirmed = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": confirm_entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["message"], "Rows to use confirmed.")
        old = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}"
        )
        self.assertEqual(old.json()["follow_up_status"], "superseded", old.text)
        confirmed_page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertIn("1 row confirmed", confirmed_page.text)

        operation_id = str(uuid4())
        recheck_values = dict(current_values, action="draft", operation_id=operation_id)
        with patch(
            "impodo.application.workspace.mapping.row_inclusion_review.stage_browser_mapping",
            side_effect=checked_rows,
        ):
            repeated = self.client.post(
                f"/workspaces/{workspace_id}/mapping/save",
                json={
                    "entries": [
                        [name, recheck_values.get(name, value)]
                        for name, value in entries
                    ]
                },
                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        confirmed_url = (
            f"/workspaces/{workspace_id}/mapping?step=confirm#confirm-matches"
        )
        self.assertEqual(repeated.json()["redirect_url"], confirmed_url)
        recovered = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}"
        )
        self.assertEqual(recovered.json()["redirect_url"], confirmed_url)

    def test_failed_rows_check_preserves_the_saved_revision_for_retry(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
        )
        context = self.app.state.context
        source_identity, source_value = dataset.columns
        operation_id = str(uuid4())
        entries = [
            ["csrf_token", self.csrf],
            ["action", "draft"],
            ["operation_id", operation_id],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["row_inclusion_mode_0", "matching_rows"],
            ["row_inclusion_join_0", "all"],
            ["row_inclusion_condition_id_0_0", str(uuid4())],
            ["row_inclusion_source_0_0", source_value.stable_key],
            ["row_inclusion_operator_0_0", "equals"],
            ["row_inclusion_value_0_0", "Example"],
            ["row_inclusion_type_0_0", "string"],
        ]

        with patch(
            "impodo.application.workspace.mapping.row_inclusion_review."
            "stage_browser_mapping",
            side_effect=WorkspaceError(
                "Included Product rows contain an invalid value."
            ),
        ):
            checked = self.client.post(
                f"/workspaces/{workspace_id}/mapping/save",
                json={"entries": entries},
                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            )

        self.assertEqual(checked.status_code, 422, checked.text)
        self.assertEqual(checked.json()["status"], "rejected")
        self.assertEqual(
            checked.json()["failure_code"],
            "MAPPING_ROW_REVIEW_FAILED",
        )
        self.assertTrue(checked.json()["partial_save"])
        self.assertIn("field matches were saved", checked.json()["detail"])
        self.assertIn("invalid value", checked.json()["detail"])
        revision = context.queries.get_mapping_revision(workspace_id)
        working = context.queries.get_mapping_working_draft(workspace_id)
        self.assertIsNotNone(revision)
        self.assertIsNotNone(working)
        self.assertEqual(
            checked.json()["expected_parent_version"],
            revision.version,
        )
        self.assertEqual(
            checked.json()["expected_working_draft_version"],
            working.version,
        )

        receipt = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}"
        )
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["status"], "rejected")
        self.assertTrue(receipt.json()["partial_save"])
        self.assertIn("select Check matches again", receipt.json()["detail"])
        snapshot, _confirmation = context.row_inclusion_reviews.current(
            workspace_id,
            actor=resolve_local_actor(context),
        )
        self.assertIsNone(snapshot)
