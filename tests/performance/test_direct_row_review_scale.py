"""Qualify direct row review and bounded session parity at 100,000 rows."""

from time import perf_counter
import unittest

import psutil

from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.preparation.bounded_preparation import (
    prepare_bounded_direct_session,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.artifacts import test_direct_row_review as fixtures
from tests.integration.artifacts.test_source_snapshot_io import COLUMNAR_TRANSFORMATIONS
from tests.support.database_probe import count_duckdb_statements


class DirectRowReviewScaleTests(unittest.TestCase):
    def test_100000_rows_review_publish_and_prepare_agree(self):
        fixture = fixtures.DirectRowReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.configure(100_000)
        started = perf_counter()
        with (
            collect_duckdb_request_timings() as timing,
            count_duckdb_statements() as sql,
        ):
            snapshot = run_local_operation_sync(
                fixture.service.check_current,
                fixture.workspace.workspace_id,
                actor=LOCAL_ACTOR,
            )
        review_seconds = perf_counter() - started
        self.assertEqual(snapshot.source_row_count, 100_000)
        self.assertEqual(
            (
                snapshot.included_count,
                snapshot.excluded_count,
                snapshot.cannot_evaluate_count,
            ),
            (90_000, 10_000, 0),
        )
        self.assertEqual(timing.connection_count, 1)
        self.assertEqual(timing.schema_check_count, 1)
        self.assertEqual(timing.lock_retry_count, 0)
        # 100 bounded row INSERTs and fewer than 45 fixed schema/audit queries.
        self.assertLessEqual(len(sql), 145)
        self.assertEqual(
            sum(
                "INSERT INTO mapping_row_inclusion_review_row" in statement
                for statement in sql
            ),
            100,
        )
        fixture.reviews.confirm_current_review(
            fixture.workspace.workspace_id, snapshot, actor=LOCAL_ACTOR
        )
        sessions = PreparationSessionRepository(fixture.database, fixture.artifacts)
        prepared = run_local_operation_sync(
            prepare_bounded_direct_session,
            fixture.workspace,
            fixture.definition,
            1,
            fixture.selection,
            fixture.selection,
            (fixture.catalog,),
            fixture.artifacts,
            None,
            sessions,
            COLUMNAR_TRANSFORMATIONS,
            actor=LOCAL_ACTOR,
            source_snapshots=(fixture.snapshot,),
        )
        self.assertEqual(
            prepared.run.reconciliation.total_rows, snapshot.source_row_count
        )
        self.assertEqual(
            prepared.run.reconciliation.candidate_rows, snapshot.included_count
        )
        self.assertEqual(
            prepared.run.reconciliation.excluded_rows, snapshot.excluded_count
        )
        observed_mib = psutil.Process().memory_info().rss / (1024 * 1024)
        print(
            f"100,000-row Parquet review: {review_seconds:.3f}s, "
            f"{timing.connection_count} connection, {len(sql)} SQL statements; "
            f"review and preparation {perf_counter() - started:.3f}s, "
            f"observed RSS {observed_mib:.1f} MiB",
            flush=True,
        )


if __name__ == "__main__":
    unittest.main()
