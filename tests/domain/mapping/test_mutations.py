"""Unit evidence for Match data browser mutation receipts."""

from __future__ import annotations

from datetime import UTC, datetime
import unittest

from impodo.domain.mapping.mutations import (
    MappingMutationAction,
    MappingMutationReceipt,
    MappingMutationState,
    MappingVersionConflict,
)
from impodo.domain.workspace.errors import WorkspaceError


class MappingMutationReceiptTests(unittest.TestCase):
    def test_pending_receipt_has_a_bounded_portable_shape(self) -> None:
        receipt = MappingMutationReceipt(
            operation_id="10000000-0000-4000-8000-000000000001",
            workspace_id="20000000-0000-4000-8000-000000000001",
            action=MappingMutationAction.SAVE_PROGRESS,
            request_hash="0" * 64,
            state=MappingMutationState.PENDING,
            submitted_working_draft_version=4,
            submitted_mapping_revision_version=2,
            working_draft_version=None,
            mapping_revision_version=None,
            content_identity="",
            failure_code="",
            failure_detail="",
            started_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
            completed_at=None,
            actor_issuer="impodo-local",
            actor_subject="data-manager",
        )

        self.assertEqual(receipt.portable_dict()["status"], "pending")
        self.assertEqual(
            receipt.portable_dict()["submitted_working_draft_version"],
            4,
        )
        self.assertNotIn("request_hash", receipt.portable_dict())
        self.assertNotIn("actor_subject", receipt.portable_dict())

    def test_terminal_receipt_requires_completion_time(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "completion time"):
            MappingMutationReceipt(
                operation_id="10000000-0000-4000-8000-000000000001",
                workspace_id="20000000-0000-4000-8000-000000000001",
                action=MappingMutationAction.CHECK_MATCHES,
                request_hash="0" * 64,
                state=MappingMutationState.COMMITTED,
                submitted_working_draft_version=4,
                submitted_mapping_revision_version=2,
                working_draft_version=5,
                mapping_revision_version=3,
                content_identity="sha256:" + "a" * 64,
                failure_code="",
                failure_detail="",
                started_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
                completed_at=None,
                actor_issuer="impodo-local",
                actor_subject="data-manager",
            )

    def test_version_conflict_keeps_submitted_and_current_versions(self) -> None:
        conflict = MappingVersionConflict(
            submitted_working_draft_version=3,
            submitted_mapping_revision_version=1,
            current_working_draft_version=4,
            current_mapping_revision_version=2,
        )

        self.assertEqual(conflict.code, "MAPPING_VERSION_CONFLICT")
        self.assertEqual(conflict.submitted_working_draft_version, 3)
        self.assertEqual(conflict.current_working_draft_version, 4)


if __name__ == "__main__":
    unittest.main()
