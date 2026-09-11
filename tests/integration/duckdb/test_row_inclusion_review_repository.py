from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.row_inclusion_review_repository import (
    RowInclusionReviewRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import (
    WorkspaceStateRepository,
)
from impodo.domain.mapping.row_inclusion_review import (
    RowInclusionDatasetReview,
    RowInclusionReviewFilter,
    RowInclusionReviewIdentity,
    RowInclusionReviewOutcome,
    RowInclusionReviewReport,
    RowInclusionReviewRow,
    RowInclusionSourceValue,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus
from tests.support.paths import REPOSITORY_ROOT


class RowInclusionReviewRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = REPOSITORY_ROOT / ".tmp" / f"row-review-{uuid4()}"
        self.temporary.mkdir()
        self.database = DuckDbWorkspaceDatabase(self.temporary)
        self.workspace_id = str(uuid4())
        WorkspaceStateRepository(self.database).initialize_workbench(
            WorkspaceState(
                workspace_id=self.workspace_id,
                name="Product row review",
                source_system="CSV",
                status=WorkspaceStatus.REGISTERED,
            ),
            actor=LOCAL_ACTOR,
        )
        self.repository = RowInclusionReviewRepository(self.database)

    def tearDown(self) -> None:
        shutil.rmtree(Path(self.temporary), ignore_errors=True)

    def test_review_pages_and_confirmation_are_bound_to_exact_inputs(self) -> None:
        identity = RowInclusionReviewIdentity(
            physical_selection_hash="sha256:" + "1" * 64,
            source_selection_hash="sha256:" + "2" * 64,
            mapping_content_hash="sha256:" + "3" * 64,
            schema_hash="sha256:" + "4" * 64,
            derived_plan_hash=None,
        )
        def value(text: str) -> tuple[RowInclusionSourceValue, ...]:
            return (
                RowInclusionSourceValue(
                    source_column_key="column:status",
                    source_column_label="Code statut product",
                    value=text,
                ),
            )
        sentence = "Include a row when Code statut product is exactly 30."
        report = RowInclusionReviewReport(
            identity=identity,
            datasets=(
                RowInclusionDatasetReview(
                    dataset_id="dataset:products",
                    dataset_name="Products",
                    source_row_count=3,
                    included_count=1,
                    excluded_count=2,
                    cannot_evaluate_count=0,
                    rule_sentence=sentence,
                ),
            ),
            rows=(
                RowInclusionReviewRow(
                    "dataset:products",
                    "Products",
                    1,
                    value("30"),
                    RowInclusionReviewOutcome.INCLUDED,
                    sentence,
                ),
                RowInclusionReviewRow(
                    "dataset:products",
                    "Products",
                    2,
                    value("20"),
                    RowInclusionReviewOutcome.EXCLUDED,
                    sentence,
                ),
                RowInclusionReviewRow(
                    "dataset:products",
                    "Products",
                    3,
                    value("40"),
                    RowInclusionReviewOutcome.EXCLUDED,
                    sentence,
                ),
            ),
        )

        snapshot = self.repository.replace_current_review(
            self.workspace_id,
            report,
            actor=LOCAL_ACTOR,
        )
        page = self.repository.get_review_page(
            self.workspace_id,
            snapshot.snapshot_hash,
            RowInclusionReviewFilter(outcome="excluded"),
            page_size=1,
        )

        self.assertEqual((snapshot.included_count, snapshot.excluded_count), (1, 2))
        self.assertEqual(page.matching_count, 2)
        self.assertEqual(page.rows[0].values[0].value, "20")
        self.assertFalse(
            self.repository.is_confirmed(
                self.workspace_id,
                mapping_content_hash=identity.mapping_content_hash,
                source_selection_hash=identity.source_selection_hash,
            )
        )

        with self.assertRaisesRegex(WorkspaceError, "mapping changed"):
            self.repository.confirm_current_review(
                self.workspace_id,
                snapshot,
                actor=LOCAL_ACTOR,
                working_draft_version=1,
                mapping_revision_version=1,
            )

        confirmation = self.repository.confirm_current_review(
            self.workspace_id,
            snapshot,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(confirmation.snapshot_hash, snapshot.snapshot_hash)
        self.assertTrue(
            self.repository.is_confirmed(
                self.workspace_id,
                mapping_content_hash=identity.mapping_content_hash,
                source_selection_hash=identity.source_selection_hash,
            )
        )
        stale = RowInclusionReviewIdentity(
            physical_selection_hash=identity.physical_selection_hash,
            source_selection_hash=identity.source_selection_hash,
            mapping_content_hash="sha256:" + "9" * 64,
            schema_hash=identity.schema_hash,
            derived_plan_hash=None,
        )
        self.assertIsNone(
            self.repository.get_current_review(self.workspace_id, stale)
        )

    def test_zero_included_rows_cannot_be_confirmed(self) -> None:
        identity = RowInclusionReviewIdentity(
            physical_selection_hash="sha256:" + "1" * 64,
            source_selection_hash="sha256:" + "2" * 64,
            mapping_content_hash="sha256:" + "3" * 64,
            schema_hash="sha256:" + "4" * 64,
            derived_plan_hash=None,
        )
        sentence = "Include a row when Status is exactly 30."
        report = RowInclusionReviewReport(
            identity=identity,
            datasets=(
                RowInclusionDatasetReview(
                    "dataset:products",
                    "Products",
                    1,
                    0,
                    1,
                    0,
                    sentence,
                ),
            ),
            rows=(
                RowInclusionReviewRow(
                    "dataset:products",
                    "Products",
                    1,
                    (
                        RowInclusionSourceValue(
                            "column:status",
                            "Status",
                            "20",
                        ),
                    ),
                    RowInclusionReviewOutcome.EXCLUDED,
                    sentence,
                ),
            ),
        )
        snapshot = self.repository.replace_current_review(
            self.workspace_id,
            report,
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(WorkspaceError, "at least one"):
            self.repository.confirm_current_review(
                self.workspace_id,
                snapshot,
                actor=LOCAL_ACTOR,
            )


if __name__ == "__main__":
    unittest.main()
