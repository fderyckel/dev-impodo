"""Direct row reviews use narrow verified Parquet with atomic bulk publication."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.mapping_repository import MappingRepository
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.row_inclusion_review_repository import (
    RowInclusionReviewRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.data_version import source_snapshots as snapshot_reader
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotPublisher,
    open_source_snapshot_batches,
)
from impodo.application.workspace.mapping.bounded_direct_review import (
    direct_row_inclusion_review,
)
from impodo.application.workspace.mapping.row_inclusion_review import (
    RowInclusionReviewService,
)
from impodo.application.workspace.preparation.preparation_service import (
    stage_browser_mapping,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import RowInclusionCondition, RowInclusionPolicy
from impodo.domain.mapping.mutations import MappingMutationAction, MappingMutationState
from impodo.domain.mapping.row_inclusion_review import RowInclusionReviewFilter
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.artifacts import test_source_snapshot_io as fixtures
from tests.support.database_probe import count_duckdb_statements


class DirectRowReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = DuckDbWorkspaceDatabase(self.root)
        self.artifacts = LocalArtifactStore(self.root)
        # Reuse registration without its legacy repository-local temporary setup.
        self.fixture = fixtures.SourceSnapshotIngestionTests()
        self.fixture.workspace_states = WorkspaceStateRepository(self.database)
        self.fixture.artifacts = self.artifacts
        self.reviews = RowInclusionReviewRepository(self.database)

    def configure(self, row_count=3, *, content=None, target_model="x_generic.record"):
        if content is None:
            content = b"Code,Name,Active\n" + b"".join(
                f"{i:x},A,{'f' if i % 10 == 1 else 't'}\n".encode()
                for i in range(row_count)
            )
        self.workspace, self.file, self.catalog = self.fixture._registered_csv(content)
        self.selection = fixtures._selection_for(
            self.workspace, self.file, self.catalog
        )
        self.snapshot = (
            SourceSnapshotPublisher(self.artifacts)
            .publish(
                self.workspace,
                self.selection,
                self.selection.datasets[0],
                self.catalog,
                self.file,
            )
            .snapshot
        )
        definition = fixtures._direct_mapping(self.selection)
        self.key = self.selection.datasets[0].columns[2].stable_key
        self.definition = replace(
            definition,
            datasets=(
                replace(
                    definition.datasets[0],
                    target_model=target_model,
                    row_inclusion=RowInclusionPolicy(
                        mode="matching_rows",
                        conditions=(
                            RowInclusionCondition(
                                str(uuid4()), self.key, "equals", "t"
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.context = SimpleNamespace(
            workspace_state=self.workspace,
            physical_selection=self.selection,
            effective_selection=self.selection,
            plan=None,
            revision=SimpleNamespace(definition=self.definition, version=1),
        )
        sources = SimpleNamespace(
            get_source_catalogs=lambda _: (self.catalog,),
            get_current_source_snapshots=lambda _: (self.snapshot,),
        )
        checked = SimpleNamespace(
            context=lambda _: self.context,
            mapping_review_context=lambda _: self.context,
            sources=sources,
            artifacts=self.artifacts,
            grouped_exact_joins=None,
            mappings=SimpleNamespace(get_mapping_working_draft=lambda _: None),
        )
        self.service = RowInclusionReviewService(checked, self.reviews, Mock())

    def review(self, definition=None):
        return direct_row_inclusion_review(
            self.workspace,
            definition or self.definition,
            self.selection,
            self.selection,
            (self.catalog,),
            self.artifacts,
            (self.snapshot,),
        )

    def test_54191_rows_preview_publish_page_confirm_and_retry(self):
        self.configure(54_191)
        self.artifacts.delete_source(
            self.selection.data_version_id, self.file.stored_name
        )
        operation_id = str(uuid4())
        mappings = MappingRepository(self.database)
        mappings.begin_mapping_mutation(
            self.workspace.workspace_id,
            operation_id=operation_id,
            action=MappingMutationAction.CHECK_MATCHES,
            request_hash="0" * 64,
            submitted_working_draft_version=None,
            submitted_mapping_revision_version=None,
            actor=LOCAL_ACTOR,
        )
        with (
            patch(
                "impodo.application.workspace.mapping.row_inclusion_review.stage_browser_mapping",
                side_effect=AssertionError("materialized route used"),
            ),
            patch(
                "impodo.application.workspace.mapping.bounded_direct_review.open_source_snapshot_batches",
                wraps=open_source_snapshot_batches,
            ) as reads,
            patch.object(
                snapshot_reader,
                "_frame_source_rows",
                wraps=snapshot_reader._frame_source_rows,
            ) as frames,
            collect_duckdb_request_timings() as timing,
            count_duckdb_statements() as sql,
        ):
            snapshot = run_local_operation_sync(
                self.service.check_current,
                self.workspace.workspace_id,
                actor=LOCAL_ACTOR,
                operation_id=operation_id,
            )
        self.assertEqual(
            (
                snapshot.source_row_count,
                snapshot.included_count,
                snapshot.excluded_count,
            ),
            (54_191, 48_772, 5_419),
        )
        self.assertEqual(snapshot.cannot_evaluate_count, 0)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(
            reads.call_args.kwargs["source_column_keys"], frozenset({self.key})
        )
        column = self.snapshot.schema.columns[2]
        for call in frames.call_args_list:
            self.assertEqual(
                call.args[0].columns,
                [
                    self.snapshot.schema.source_row_column,
                    column.value_column,
                    column.kind_column,
                ],
            )
            self.assertLessEqual(call.args[0].height, 1_000)
        # The engine has one owner. Publication uses 55
        # bounded INSERT SELECTs; schema/audit/current/receipt queries stay fixed.
        self.assertEqual(timing.connection_count, 1)
        self.assertEqual(timing.schema_check_count, 1)
        self.assertEqual(timing.lock_retry_count, 0)
        self.assertLessEqual(len(sql), 100)
        inserts = [
            s for s in sql if "INSERT INTO mapping_row_inclusion_review_row" in s
        ]
        self.assertEqual(len(inserts), 55)
        receipt = mappings.get_mapping_mutation_receipt(
            self.workspace.workspace_id, operation_id
        )
        self.assertEqual(receipt.state, MappingMutationState.COMMITTED)
        self.assertEqual(receipt.content_identity, snapshot.snapshot_hash)
        preview = self.service.preview_definition(
            self.workspace.workspace_id, self.definition, actor=LOCAL_ACTOR
        )
        self.assertEqual(preview.content_hash, snapshot.snapshot_hash)
        page = self.reviews.get_review_page(
            self.workspace.workspace_id,
            snapshot.snapshot_hash,
            RowInclusionReviewFilter(outcome="excluded"),
            page_size=2,
        )
        self.assertEqual(page.matching_count, 5_419)
        self.assertEqual([row.source_row for row in page.rows], [3, 13])
        confirmed = self.reviews.confirm_current_review(
            self.workspace.workspace_id, snapshot, actor=LOCAL_ACTOR
        )
        repeated = run_local_operation_sync(
            self.service.check_current,
            self.workspace.workspace_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(repeated, snapshot)
        self.assertEqual(
            self.reviews.get_confirmation(
                self.workspace.workspace_id, snapshot.snapshot_hash
            ),
            confirmed,
        )

    def test_narrow_review_matches_oracle_for_types_errors_and_unrelated_models(self):
        self.configure(
            content=b"Code,Name,Active\nA,First,10\nB,Second,2\nC,Third,bad\nD,Fourth,\n"
        )
        for model in ("res.partner", "stock.lot", "x_equipment.record"):
            for join in ("all", "any"):
                with self.subTest(model=model, join=join):
                    definition = replace(
                        self.definition,
                        datasets=(
                            replace(
                                self.definition.datasets[0],
                                target_model=model,
                                row_inclusion=RowInclusionPolicy(
                                    mode="matching_rows",
                                    join=join,
                                    conditions=(
                                        RowInclusionCondition(
                                            str(uuid4()),
                                            self.key,
                                            "greater_than",
                                            "5",
                                            "integer",
                                        ),
                                        RowInclusionCondition(
                                            str(uuid4()), self.key, "is_not_blank"
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    )
                    oracle = stage_browser_mapping(
                        self.workspace,
                        definition,
                        self.selection,
                        self.selection,
                        None,
                        (self.catalog,),
                        self.artifacts,
                        source_snapshots=(self.snapshot,),
                    ).row_inclusion_review
                    actual = self.review(definition)
                    self.assertEqual(actual, oracle)
                    self.assertEqual(actual.content_hash, oracle.content_hash)

    def test_projection_preserves_values_and_rejects_unknown_columns(self):
        self.configure()
        with self.artifacts.materialize_source_snapshot(
            self.selection.data_version_id,
            self.snapshot.parquet_storage_key,
            expected_sha256=self.snapshot.parquet_sha256,
        ) as path:
            with open_source_snapshot_batches(path, self.snapshot) as source:
                full = [r for batch in source.iter_batches() for r in batch]
            with open_source_snapshot_batches(
                path, self.snapshot, source_column_keys=frozenset({self.key})
            ) as source:
                narrow = [r for batch in source.iter_batches() for r in batch]
            self.assertEqual([r.number for r in narrow], [r.number for r in full])
            self.assertEqual(
                [dict(r.values) for r in narrow],
                [{"Active": r.values["Active"]} for r in full],
            )
            with self.assertRaisesRegex(SourceLoadError, "unknown columns"):
                with open_source_snapshot_batches(
                    path, self.snapshot, source_column_keys=frozenset({"unknown"})
                ):
                    pass

    def test_failed_input_and_failed_publication_preserve_existing_review(self):
        self.configure()
        before = self.reviews.replace_current_review(
            self.workspace.workspace_id, self.review(), actor=LOCAL_ACTOR
        )
        original = self.snapshot
        self.snapshot = replace(
            original,
            physical_selection_hash="sha256:" + "f" * 64,
            logical_hash="",
            parquet_storage_key="",
            _calculate_bindings=True,
        )
        with self.assertRaisesRegex(ReadinessError, "snapshot could not be verified"):
            self.review()
        self.snapshot = original
        changed = replace(self.definition, schema_hash="sha256:" + "e" * 64)
        with self.assertRaisesRegex(WorkspaceError, "receipt is not pending"):
            self.reviews.replace_current_review(
                self.workspace.workspace_id,
                self.review(changed),
                actor=LOCAL_ACTOR,
                operation_id=str(uuid4()),
            )
        self.assertEqual(
            self.reviews.get_current_review(
                self.workspace.workspace_id, before.identity
            ),
            before,
        )
        with self.artifacts.materialize_source_snapshot(
            self.selection.data_version_id,
            original.parquet_storage_key,
            expected_sha256=original.parquet_sha256,
        ) as path:
            path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ReadinessError, "snapshot could not be verified"):
            self.review()
        self.assertEqual(
            self.reviews.get_current_review(
                self.workspace.workspace_id, before.identity
            ),
            before,
        )


if __name__ == "__main__":
    unittest.main()
