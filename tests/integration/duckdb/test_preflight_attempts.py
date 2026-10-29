"""Verify restart-safe local Final review attempt persistence."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.application.preflight_attempts import (
    PreflightComparisonReceipt,
    PreflightComparisonRequest,
)
from impodo.application.preflight_jobs import PreflightJobManager
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationOperationReplayError,
    MigrationOperationState,
)
from impodo.domain.shared.access import LOCAL_ACTOR

PROJECT_ID = "10000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
DATA_VERSION_ID = "30000000-0000-4000-8000-000000000001"
MIGRATION_RUN_ID = "40000000-0000-4000-8000-000000000001"
OPERATION_ID = "50000000-0000-4000-8000-000000000001"
OTHER_OPERATION_ID = "60000000-0000-4000-8000-000000000001"


def _request(
    *,
    operation_id: str = OPERATION_ID,
    semantic_input_hash: str = "sha256:" + "1" * 64,
) -> PreflightComparisonRequest:
    return PreflightComparisonRequest(
        operation_id=operation_id,
        project_id=PROJECT_ID,
        workspace_id=WORKSPACE_ID,
        semantic_input_hash=semantic_input_hash,
        workspace_revision=3,
        data_version_id=DATA_VERSION_ID,
        migration_run_id=MIGRATION_RUN_ID,
        recipe_application_id=None,
        requested_at=datetime(2026, 10, 29, 12, tzinfo=UTC),
    )


class PreflightAttemptRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        database = MigrationFoundationDatabase(Path(self.temporary.name))
        self.repository = MigrationFoundationRepository(database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_replay_reuses_attempt_and_changed_meaning_fails_closed(
        self,
    ) -> None:
        first = self.repository.reserve_preflight_comparison(
            _request(),
            actor=LOCAL_ACTOR,
        )
        replayed = self.repository.reserve_preflight_comparison(
            _request(),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(replayed.operation_id, first.operation_id)
        self.assertEqual(replayed.state, MigrationOperationState.PENDING)
        with self.assertRaises(MigrationOperationReplayError):
            self.repository.reserve_preflight_comparison(
                replace(
                    _request(),
                    semantic_input_hash="sha256:" + "2" * 64,
                ),
                actor=LOCAL_ACTOR,
            )

    def test_only_one_workspace_attempt_is_pending_and_receipt_is_idempotent(
        self,
    ) -> None:
        self.repository.reserve_preflight_comparison(
            _request(),
            actor=LOCAL_ACTOR,
        )
        with self.assertRaises(MigrationConflictError):
            self.repository.reserve_preflight_comparison(
                _request(operation_id=OTHER_OPERATION_ID),
                actor=LOCAL_ACTOR,
            )

        advanced = self.repository.advance_preflight_comparison(
            OPERATION_ID,
            WORKSPACE_ID,
            "READING",
        )
        self.assertEqual(advanced.stage, "READING")
        result = {
            "manifest_hash": "sha256:" + "3" * 64,
            "preflight_run_id": "70000000-0000-4000-8000-000000000001",
        }
        committed = self.repository.commit_preflight_comparison(
            OPERATION_ID,
            WORKSPACE_ID,
            result=result,
        )
        replayed = self.repository.commit_preflight_comparison(
            OPERATION_ID,
            WORKSPACE_ID,
            result=result,
        )

        self.assertEqual(committed.state, MigrationOperationState.COMMITTED)
        self.assertEqual(replayed.result, result)
        self.assertEqual(
            self.repository.list_pending_preflight_comparisons(),
            (),
        )

    def test_interrupted_attempt_retains_only_safe_terminal_code(self) -> None:
        self.repository.reserve_preflight_comparison(
            _request(),
            actor=LOCAL_ACTOR,
        )
        failed = self.repository.fail_preflight_comparison(
            OPERATION_ID,
            WORKSPACE_ID,
            failure_code="COMPARISON_INTERRUPTED",
            stage="INTERRUPTED",
        )

        self.assertEqual(failed.state, MigrationOperationState.FAILED)
        self.assertEqual(failed.stage, "INTERRUPTED")
        self.assertEqual(failed.last_error, "COMPARISON_INTERRUPTED")
        self.assertEqual(failed.result, {})

    def test_restart_recovery_commits_only_a_verified_matching_receipt(self) -> None:
        self.repository.reserve_preflight_comparison(
            _request(),
            actor=LOCAL_ACTOR,
        )
        manager = PreflightJobManager(self.repository)
        receipt = PreflightComparisonReceipt(
            operation_id=OPERATION_ID,
            workspace_id=WORKSPACE_ID,
            preflight_run_id="70000000-0000-4000-8000-000000000001",
            frozen_input_hash="sha256:" + "2" * 64,
            requirement_plan_hash="sha256:" + "3" * 64,
            result_hash="sha256:" + "4" * 64,
            manifest_hash="sha256:" + "5" * 64,
            execution_snapshot_hash="sha256:" + "6" * 64,
        )

        manager.reconcile_pending(lambda _workspace_id, _operation_id: receipt)
        recovered = manager.get(WORKSPACE_ID, OPERATION_ID)

        self.assertEqual(recovered.status.value, "SUCCEEDED")
        self.assertEqual(recovered.preflight_run_id, receipt.preflight_run_id)
        self.assertEqual(
            self.repository.get_preflight_comparison(
                OPERATION_ID,
                WORKSPACE_ID,
            ).result["manifest_hash"],
            receipt.manifest_hash,
        )

    def test_restart_without_matching_workspace_commit_is_interrupted(self) -> None:
        self.repository.reserve_preflight_comparison(
            _request(),
            actor=LOCAL_ACTOR,
        )
        manager = PreflightJobManager(self.repository)

        manager.reconcile_pending(lambda _workspace_id, _operation_id: None)
        recovered = manager.get(WORKSPACE_ID, OPERATION_ID)

        self.assertEqual(recovered.status.value, "FAILED")
        self.assertIsNotNone(recovered.failure)
        assert recovered.failure is not None
        self.assertEqual(recovered.failure.code.value, "COMPARISON_INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
