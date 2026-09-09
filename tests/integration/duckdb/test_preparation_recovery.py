"""Recover a published duplicate review through the real resolution lifecycle."""

from unittest import TestCase
from uuid import uuid4

from impodo.adapters.duckdb.preparation_recovery_repository import PreparationRecoveryRepository
from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.application.workspace.preparation.recovery import PreparationRecoveryInputs
from impodo.domain.preparation.quality import retention_context_hash
from impodo.domain.resolution import (
    ResolutionDecision, ResolutionDecisionKind, build_effective_dataset,
    evaluate_resolution_candidates,
)
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.application.workspace.mapping import test_advanced_coverage as fixtures


class DuplicateReviewRecoveryTests(TestCase):
    def test_current_duplicate_review_recovers_until_approved_or_invalidated(self):
        fixture = fixtures.AdvancedCoveragePersistenceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id = fixture.workspace_state.workspace_id
        repository = fixture.repository
        scope = fixtures._scope(workspace_id=workspace_id)
        references = ReferenceBundle(workspace_id=workspace_id, datasets=())
        policy = fixtures._policy(
            workspace_id=workspace_id, coverage_scope_hash=scope.content_hash,
            mapping_hash=fixtures.HASH_C, schema_hash=fixtures.HASH_D,
            reference_bundle_hash=references.content_hash,
        )
        repository.save_coverage_scope(workspace_id, scope, expected_parent_version=None, actor=LOCAL_ACTOR)
        repository.save_reference_bundle(workspace_id, references, actor=LOCAL_ACTOR)
        repository.save_resolution_policy(workspace_id, policy, expected_parent_version=None, actor=LOCAL_ACTOR)
        rows = (fixtures._row(1, name="Acme SA", street="1 Main Street"),
                fixtures._row(2, name="ACME S.A.", street="Main Street 1"))
        staging = fixture.staging.publish_canonical_staging(
            workspace_id, fixtures._staging_run(workspace_id, rows), mapping_version=1, actor=LOCAL_ACTOR,
        )
        evaluation = evaluate_resolution_candidates(policy=policy, staging_content_hash=staging.content_hash, rows=rows)
        summary = repository.publish_resolution_evaluation(workspace_id, evaluation, staging_run_id=staging.run_id, actor=LOCAL_ACTOR)
        reader = PreparationRecoveryRepository(fixture.database)
        inputs = PreparationRecoveryInputs(fixtures.HASH_C, fixtures.HASH_A, fixtures.HASH_A, fixtures.HASH_D,
                                           retention_context_hash(fixture.workspace_state))
        result = reader.read_current(workspace_id, inputs)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PreparationJobStatus.REVIEW_REQUIRED)
        self.assertEqual((result.staging_run_id, result.result_run_id), (staging.run_id, summary.run_id))

        path = repository.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        for column, value in (("status", "INVALIDATED"), ("policy_hash", fixtures.HASH_A),
                              ("staging_content_hash", fixtures.HASH_B)):
            with self.subTest(stale=column):
                with repository._connect(path) as connection:
                    original = connection.execute(f"SELECT {column} FROM resolution_run WHERE run_id = ?", [summary.run_id]).fetchone()[0]
                    connection.execute(f"UPDATE resolution_run SET {column} = ? WHERE run_id = ?", [value, summary.run_id])
                try:
                    self.assertIsNone(reader.read_current(workspace_id, inputs))
                finally:
                    with repository._connect(path) as connection:
                        connection.execute(f"UPDATE resolution_run SET {column} = ? WHERE run_id = ?", [original, summary.run_id])

        candidate = evaluation.candidates[0]
        decision = ResolutionDecision(
            decision_id=str(uuid4()), evaluation_hash=evaluation.content_hash,
            group_id=candidate.candidate_id, kind=ResolutionDecisionKind.KEEP_SEPARATE,
            row_ids=(candidate.left_row_id, candidate.right_row_id), reason="Two separate legal entities.",
            actor=LOCAL_ACTOR.identity, decided_at=fixtures.NOW, lifecycle_version=1,
        )
        repository.append_resolution_decision(workspace_id, summary.run_id, decision,
                                              expected_lifecycle_version=0, actor=LOCAL_ACTOR)
        effective = build_effective_dataset(policy=policy, evaluation=evaluation, rows=rows, decisions=(decision,))
        repository.freeze_effective_dataset(workspace_id, summary.run_id, effective,
                                            expected_lifecycle_version=1, actor=LOCAL_ACTOR)
        self.assertIsNone(reader.read_current(workspace_id, inputs), "Approved duplicates need preparation to continue")
