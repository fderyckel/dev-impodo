from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.mapping_repository import MappingRepository
from impodo.adapters.duckdb.row_inclusion_review_repository import (
    RowInclusionReviewRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import (
    WorkspaceStateRepository,
)
from impodo.domain.mapping.mutations import (
    MappingMutationAction,
    MappingMutationState,
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
        operation_id = str(uuid4())
        mapping_repository = MappingRepository(self.database)
        mapping_repository.begin_mapping_mutation(
            self.workspace_id,
            operation_id=operation_id,
            action=MappingMutationAction.CHECK_MATCHES,
            request_hash="0" * 64,
            submitted_working_draft_version=6,
            submitted_mapping_revision_version=2,
            actor=LOCAL_ACTOR,
        )

        snapshot = self.repository.replace_current_review(
            self.workspace_id,
            report,
            actor=LOCAL_ACTOR,
            operation_id=operation_id,
            working_draft_version=7,
            mapping_revision_version=3,
        )
        repeated = self.repository.replace_current_review(
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

        self.assertEqual(repeated, snapshot)
        self.assertEqual((snapshot.included_count, snapshot.excluded_count), (1, 2))
        receipt = mapping_repository.get_mapping_mutation_receipt(
            self.workspace_id,
            operation_id,
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.state, MappingMutationState.COMMITTED)
        self.assertEqual(receipt.working_draft_version, 7)
        self.assertEqual(receipt.mapping_revision_version, 3)
        self.assertEqual(receipt.content_identity, snapshot.snapshot_hash)
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
        with self.assertRaisesRegex(WorkspaceError, "receipt is not pending"):
            self.repository.replace_current_review(
                self.workspace_id,
                replace(report, identity=stale),
                actor=LOCAL_ACTOR,
                operation_id=str(uuid4()),
                working_draft_version=8,
                mapping_revision_version=4,
            )
        self.assertIsNone(
            self.repository.get_current_review(self.workspace_id, stale)
        )
        self.assertEqual(
            self.repository.get_current_review(self.workspace_id, identity),
            snapshot,
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

    def test_multiple_batches_preserve_rows_and_rollback_failed_publication(self) -> None:
        identity = RowInclusionReviewIdentity(
            physical_selection_hash="sha256:" + "1" * 64,
            source_selection_hash="sha256:" + "2" * 64,
            mapping_content_hash="sha256:" + "3" * 64,
            schema_hash="sha256:" + "4" * 64,
            derived_plan_hash=None,
        )
        sentence = "Include a row when Status is exactly 30."
        rows = tuple(
            RowInclusionReviewRow(
                "dataset:products", "Products", index + 1,
                (RowInclusionSourceValue(
                    "column:status", "Statut café", f'null "30"\n{index}',
                ),),
                RowInclusionReviewOutcome.INCLUDED,
                sentence,
            )
            for index in range(2_005)
        )
        report = RowInclusionReviewReport(
            identity=identity,
            datasets=(RowInclusionDatasetReview(
                "dataset:products", "Products", len(rows), len(rows), 0, 0,
                sentence,
            ),),
            rows=rows,
        )
        snapshot = self.repository.replace_current_review(
            self.workspace_id, report, actor=LOCAL_ACTOR,
        )
        # Read across both full-batch boundaries and the final partial batch.
        for start in (0, 995, 1_995):
            page = self.repository.get_review_page(
                self.workspace_id, snapshot.snapshot_hash,
                RowInclusionReviewFilter(), page_size=20,
                after=start - 1 if start else None,
            )
            self.assertEqual(page.rows, rows[start:start + 20])
            self.assertEqual(page.matching_count, len(rows))

        changed = replace(
            report,
            identity=replace(identity, mapping_content_hash="sha256:" + "9" * 64),
        )
        with self.assertRaisesRegex(WorkspaceError, "receipt is not pending"):
            self.repository.replace_current_review(
                self.workspace_id, changed, actor=LOCAL_ACTOR,
                operation_id=str(uuid4()),
            )
        with self.database._connect(
            self.database.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        ) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM mapping_row_inclusion_review_row"
                ).fetchone()[0],
                len(rows),
            )
        self.assertEqual(
            self.repository.get_current_review(self.workspace_id, identity),
            snapshot,
        )


if __name__ == "__main__":
    unittest.main()
