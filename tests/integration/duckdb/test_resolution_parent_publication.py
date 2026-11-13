"""Reuse resolution evidence only while its exact staging parent is current."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from impodo.domain.coverage import ReferenceBundle
from impodo.domain.resolution import evaluate_resolution_candidates
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.application.workspace.mapping import test_advanced_coverage as fixtures


class ResolutionParentPublicationTests(unittest.TestCase):
    def test_same_evaluation_reuses_only_the_same_staging_parent(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = fixtures.AdvancedCoveragePersistenceTests()
        # The shared fixture's working directory remains under the OS temp root.
        with patch.object(fixtures, "ROOT", Path(temporary.name)):
            fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id = fixture.workspace_state.workspace_id
        repository = fixture.repository
        scope = fixtures._scope(workspace_id=workspace_id)
        references = ReferenceBundle(workspace_id=workspace_id, datasets=())
        policy = fixtures._policy(
            workspace_id=workspace_id,
            coverage_scope_hash=scope.content_hash,
            mapping_hash=fixtures.HASH_C,
            schema_hash=fixtures.HASH_D,
            reference_bundle_hash=references.content_hash,
        )
        repository.save_coverage_scope(
            workspace_id, scope, expected_parent_version=None, actor=LOCAL_ACTOR
        )
        repository.save_reference_bundle(workspace_id, references, actor=LOCAL_ACTOR)
        repository.save_resolution_policy(
            workspace_id, policy, expected_parent_version=None, actor=LOCAL_ACTOR
        )
        rows = (
            fixtures._row(1, name="Acme SA", street="1 Main Street"),
            fixtures._row(2, name="ACME S.A.", street="Main Street 1"),
        )
        run = fixtures._staging_run(workspace_id, rows)
        first_staging = fixture.staging.publish_canonical_staging(
            workspace_id, run, mapping_version=1, actor=LOCAL_ACTOR
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy, staging_content_hash=first_staging.content_hash, rows=rows
        )
        first = repository.publish_resolution_evaluation(
            workspace_id,
            evaluation,
            staging_run_id=first_staging.run_id,
            actor=LOCAL_ACTOR,
        )
        unchanged = repository.publish_resolution_evaluation(
            workspace_id,
            evaluation,
            staging_run_id=first_staging.run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(unchanged.run_id, first.run_id)

        path = repository.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        # Inject a lost staging pointer while leaving the old resolution intact.
        # A new valid parent may have the exact same canonical content.
        with repository._connect(path) as connection:
            connection.execute("DELETE FROM canonical_staging_current")
        second_staging = fixture.staging.publish_canonical_staging(
            workspace_id, run, mapping_version=1, actor=LOCAL_ACTOR
        )
        self.assertNotEqual(second_staging.run_id, first_staging.run_id)
        self.assertEqual(second_staging.content_hash, first_staging.content_hash)
        second = repository.publish_resolution_evaluation(
            workspace_id,
            evaluation,
            staging_run_id=second_staging.run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertNotEqual(second.run_id, first.run_id)
        self.assertEqual(second.evaluation_hash, first.evaluation_hash)
        self.assertEqual(second.staging_run_id, second_staging.run_id)
        repeated = repository.publish_resolution_evaluation(
            workspace_id,
            evaluation,
            staging_run_id=second_staging.run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(repeated.run_id, second.run_id)
        old = repository.get_resolution_summary(workspace_id, first.run_id)
        self.assertIsNotNone(old)
        assert old is not None
        self.assertEqual(old.staging_run_id, first_staging.run_id)
        self.assertEqual(old.evaluation_hash, first.evaluation_hash)
        self.assertEqual(old.status, "INVALIDATED")


if __name__ == "__main__":
    unittest.main()
