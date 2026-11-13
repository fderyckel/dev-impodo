"""Keep operation telemetry and existing benchmark collectors accurate together."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.application.preflight_timing import (
    collect_preflight_publication_timings,
    timed_preflight_publication,
)
from impodo.application.shared.performance import OperationPerformance, bind_performance
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.database_probe import count_duckdb_statements


class PreflightTimingNestingTests(TestCase):
    def test_publication_inner_scope_preserves_outer_measurement_once(self):
        with collect_preflight_publication_timings() as outer:
            with collect_preflight_publication_timings() as inner:
                with timed_preflight_publication("manifest_serialization"):
                    pass
            self.assertEqual(outer.calls, {"manifest_serialization": 1})
            self.assertEqual(inner.calls, outer.calls)
            self.assertEqual(inner.milliseconds, outer.milliseconds)
            with timed_preflight_publication("manifest_serialization"):
                pass
        self.assertEqual(outer.calls["manifest_serialization"], 2)
        self.assertEqual(inner.calls["manifest_serialization"], 1)

    def test_nested_local_operations_preserve_outer_sql_and_count_once(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "fictional.duckdb"
            factory = DuckDbConnectionFactory()
            collector = OperationPerformance(
                operation_id=str(uuid4()), operation="preflight"
            )

            def read():
                with factory.connect(path) as connection:
                    return connection.execute("SELECT 42").fetchone()[0]

            def nested():
                return read(), run_local_operation_sync(read), read()

            with (
                collect_duckdb_request_timings() as outer,
                bind_performance(collector),
                count_duckdb_statements() as statements,
            ):
                self.assertEqual(run_local_operation_sync(nested), (42, 42, 42))
                self.assertEqual(outer.connection_count, 1)
                self.assertEqual(
                    dict(collector.snapshot().counters)["database_connection_count"], 1
                )
                self.assertEqual(run_local_operation_sync(read), 42)
            self.assertEqual(
                sum("SELECT 42" in statement for statement in statements), 4
            )
            self.assertEqual(outer.connection_count, 2)
            self.assertEqual(
                dict(collector.snapshot().counters)["database_connection_count"], 2
            )
            self.assertAlmostEqual(
                dict(collector.snapshot().metrics)["database_connection_ms"],
                outer.connect_ms,
                delta=0.002,
            )

    def test_failed_nested_operation_releases_scope_and_next_operation_records_independently(
        self,
    ):
        with TemporaryDirectory() as root:
            path = Path(root) / "fictional.duckdb"
            factory = DuckDbConnectionFactory()
            collector = OperationPerformance(
                operation_id=str(uuid4()), operation="preflight"
            )

            def read():
                with factory.connect(path) as connection:
                    return connection.execute("SELECT 7").fetchone()[0]

            def fail():
                self.assertEqual(read(), 7)
                raise ValueError("Fictional operation failed")

            with collect_duckdb_request_timings() as outer, bind_performance(collector):
                with self.assertRaisesRegex(ValueError, "Fictional operation failed"):
                    run_local_operation_sync(lambda: run_local_operation_sync(fail))
                self.assertEqual(outer.connection_count, 1)
                self.assertEqual(
                    dict(collector.snapshot().counters)["database_connection_count"], 1
                )
                self.assertEqual(run_local_operation_sync(read), 7)
            self.assertEqual(outer.connection_count, 2)
            self.assertEqual(
                dict(collector.snapshot().counters)["database_connection_count"], 2
            )
