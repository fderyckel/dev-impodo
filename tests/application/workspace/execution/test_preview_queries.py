"""Keep full and saved review facts aligned without changing frozen evidence."""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from impodo.application.workspace.execution.navigation import (
    ExecutionReviewProjection,
    build_execution_review_projection,
)
from impodo.application.workspace.execution.preview import build_execution_preview
from impodo.domain.execution.odoo_scope import execution_api_scope
from impodo.domain.execution_snapshot import RelationshipBlocker
from tests.application.workspace.execution.test_service import _snapshot


class ExecutionPreviewQueryTests(unittest.TestCase):
    def _report(self, snapshot, *, labels=None):
        labels = labels or {}
        return SimpleNamespace(
            run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            result_hash=snapshot.preflight_result_hash,
            target_database=snapshot.target_database,
            status="NEEDS_REVIEW",
            datasets=tuple(
                SimpleNamespace(dataset=key, label=value) for key, value in labels.items()
            ),
        )

    def test_full_and_saved_reviews_share_guidance_groups_and_preserve_hashes(self):
        for major in (19, 20):
            with self.subTest(major=major):
                snapshot = _snapshot()
                snapshot = replace(
                    snapshot,
                    target_odoo_version=f"{major}.0",
                    relationship_plan=replace(
                        snapshot.relationship_plan,
                        blockers=(
                            RelationshipBlocker(
                                row_id=snapshot.rows[0].row_id,
                                code="MISSING_INCOMING_ROW",
                            ),
                        ),
                    ),
                )
                original = snapshot.to_json()
                full = build_execution_preview(
                    snapshot,
                    current_run=None,
                    api_scope=execution_api_scope(snapshot),
                    deferred_create_count=0,
                )
                saved = build_execution_review_projection(
                    self._report(snapshot), snapshot, execution_shape_ready=False
                )
                self.assertEqual(full.dependency_summary, saved.dependency_summary)
                self.assertEqual(full.blocker_summary, saved.blocker_summary)
                blocker = full.blocker_summary.groups[0]
                self.assertEqual(blocker.title, "A related source record is missing")
                self.assertEqual(
                    blocker.action,
                    "Add the supporting record or change the relationship to a valid "
                    "reviewed record, then compare again.",
                )
                self.assertEqual(saved.summary.snapshot_hash, snapshot.semantic_hash)
                self.assertEqual(
                    ExecutionReviewProjection.from_json(saved.to_json()).to_json(),
                    saved.to_json(),
                )
                self.assertEqual(snapshot.to_json(), original)

    def test_saved_labels_do_not_replace_full_preview_dataset_identifiers(self):
        snapshot = _snapshot()
        full = build_execution_preview(
            snapshot,
            current_run=None,
            api_scope=execution_api_scope(snapshot),
            deferred_create_count=0,
        )
        saved = build_execution_review_projection(
            self._report(snapshot, labels={"products": "Product catalogue"}),
            snapshot,
            execution_shape_ready=True,
        )
        self.assertEqual(full.datasets[1].dataset, "products")
        self.assertEqual(saved.datasets[1].dataset, "Product catalogue")
        self.assertEqual(full.datasets[1].create_count, saved.datasets[1].create_count)
        self.assertEqual(
            tuple(group.record_count for group in full.dependency_summary.groups),
            tuple(group.record_count for group in saved.dependency_summary.groups),
        )
        self.assertIn(
            "Product catalogue",
            tuple(
                label
                for group in saved.dependency_summary.groups
                for label in group.dataset_labels
            ),
        )


if __name__ == "__main__":
    unittest.main()
