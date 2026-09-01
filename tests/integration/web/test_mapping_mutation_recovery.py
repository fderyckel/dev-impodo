"""Browser evidence for durable mapping-mutation recovery."""

from __future__ import annotations

from uuid import uuid4

from impodo.domain.mapping.mutations import MappingMutationAction

from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
)


class MappingMutationRecoveryBrowserTests(ProjectSetupBrowserTestCase):
    def test_save_has_a_durable_receipt_and_conflict_recovery(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=30
        )
        source_identity = dataset.columns[0]
        context = self.app.state.context
        operation_id = str(uuid4())
        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["operation_id", operation_id],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
        ]

        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            saved.json()["message"],
            "Progress saved. Check matches when ready.",
        )
        self.assertEqual(saved.json()["authoring_issues"], [])
        self.assertEqual(saved.json()["operation_id"], operation_id)
        self.assertEqual(saved.json()["status"], "committed")
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertIsNotNone(working)
        self.assertEqual(working.version, 1)
        self.assertEqual(
            working.definition.datasets[0].target_identity[0].source_column_keys,
            (source_identity.stable_key,),
        )
        self.assertIsNone(
            context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )

        receipt = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}"
        )
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["status"], "committed")
        self.assertEqual(receipt.json()["expected_working_draft_version"], 1)
        self.assertEqual(receipt.json()["content_identity"], working.content_hash)

        replayed = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json()["status"], "committed")
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            ).version,
            1,
        )

        stale_operation_id = str(uuid4())
        stale_entries = [
            [name, stale_operation_id if name == "operation_id" else value]
            for name, value in entries
        ]
        stale = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": stale_entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["failure_code"], "MAPPING_VERSION_CONFLICT")
        self.assertIsNone(stale.json()["submitted_working_draft_version"])
        self.assertEqual(stale.json()["current_working_draft_version"], 1)
        self.assertTrue(stale.json()["recovery"]["copy_edits"])
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            ).version,
            1,
        )
        stale_receipt = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/"
            f"{stale_operation_id}"
        )
        self.assertEqual(stale_receipt.json()["status"], "rejected")

        missing_operation_id = str(uuid4())
        missing = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/"
            f"{missing_operation_id}"
        )
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertEqual(missing.json()["status"], "not_found")

        pending_operation_id = str(uuid4())
        context.mapping_workspace.begin_mutation(
            workspace_id,
            operation_id=pending_operation_id,
            action=MappingMutationAction.SAVE_PROGRESS,
            request_hash="0" * 64,
            submitted_working_draft_version=1,
            submitted_mapping_revision_version=None,
            actor=context.actor,
        )
        pending = self.client.get(
            f"/workspaces/{workspace_id}/mapping/mutation-receipts/"
            f"{pending_operation_id}"
        )
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual(pending.json()["status"], "pending")


if __name__ == "__main__":
    import unittest

    unittest.main()
