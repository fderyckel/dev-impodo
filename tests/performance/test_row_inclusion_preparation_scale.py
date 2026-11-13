"""Qualify the full Parquet preparation route with a saved Rows to use rule."""

import json
import os
from time import perf_counter
import unittest

import psutil

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from tests.integration.artifacts import test_row_inclusion_preparation as integration
from tests.performance import test_preparation_scale as fixtures
from tests.support.database_probe import count_duckdb_statements


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_ROW_INCLUSION_PREPARATION_SCALE") == "1",
    "full row-inclusion preparation scale qualification is opt-in",
)
class RowInclusionPreparationScaleTests(unittest.TestCase):
    setUp = integration.RowInclusionPreparationTests.setUp
    tearDown = integration.RowInclusionPreparationTests.tearDown
    configure = integration.RowInclusionPreparationTests.configure
    prepare = integration.RowInclusionPreparationTests.prepare
    assert_counts = integration.RowInclusionPreparationTests.assert_counts

    def qualify(self, count):
        self.configure(count)
        started = perf_counter()
        previous_phase = None

        def progress(phase, completed, total, _message):
            nonlocal previous_phase
            if phase != previous_phase:
                print(
                    f"{count:,} rows: {phase.value} ({completed:,}/{total:,})",
                    flush=True,
                )
                previous_phase = phase

        with (
            fixtures._PeakWorkingSetSampler(psutil.Process()) as memory,
            collect_duckdb_request_timings() as timing,
            count_duckdb_statements() as sql,
        ):
            self.prepare(progress=progress)
        self.assert_counts(count)
        self.assertEqual(self.review.included_count, count - 1)
        print(
            json.dumps(
                {
                    "rows": count,
                    "seconds": round(perf_counter() - started, 3),
                    "peak_mib": round(memory.peak_bytes / (1024 * 1024), 1),
                    "connections": timing.connection_count,
                    "schema_checks": timing.schema_check_count,
                    "sql_statements": len(sql),
                    "lock_retries": timing.lock_retry_count,
                }
            ),
            flush=True,
        )
        self.assertEqual(timing.lock_retry_count, 0)
        # Five bounded phase scopes plus the surrounding authorized state
        # reads have a fixed owner/validation budget, independent of row count.
        self.assertLessEqual(timing.connection_count, 40)
        self.assertLessEqual(timing.schema_check_count, 28)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in sql),
            timing.schema_check_count,
        )
        # Source, quality and normalization facts use bounded bulk transports.
        # Reserve eight statements per 1,000 rows and 550 fixed setup queries.
        self.assertLessEqual(len(sql), 550 + 8 * ((count + 999) // 1000))

    def test_reported_54191_rows_pass_full_preparation(self):
        self.qualify(54_191)

    def test_100000_rows_pass_full_preparation(self):
        self.qualify(100_000)


if __name__ == "__main__":
    unittest.main()
