"""Database lifetimes for synchronous page reads, using real DuckDB files."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import (
    DuckDbConnectionFactory,
    retain_databases_for_operation,
    retain_databases_for_read,
    validate_schema_once_for_operation,
)
from tests.support.paths import REPOSITORY_ROOT


class PageReadConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = REPOSITORY_ROOT / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "page.duckdb"
        self.factory = DuckDbConnectionFactory()
        with self.factory.connect(self.path) as connection:
            connection.execute("CREATE TABLE values_to_read (value INTEGER)")
            connection.execute("INSERT INTO values_to_read VALUES (1)")

    def read(self) -> int:
        with self.factory.connect(self.path) as connection:
            return connection.execute("SELECT sum(value) FROM values_to_read").fetchone()[0]

    def test_nested_reads_open_once_and_next_request_observes_changes(self) -> None:
        with collect_duckdb_request_timings() as timings:
            with retain_databases_for_read():
                self.assertEqual(self.read(), 1)
                with retain_databases_for_read():
                    self.assertEqual(self.read(), 1)
                self.assertEqual(self.read(), 1)
            self.assertEqual(timings.connection_count, 1)
            with self.factory.connect(self.path) as connection:
                connection.execute("INSERT INTO values_to_read VALUES (2)")
            with retain_databases_for_read():
                self.assertEqual(self.read(), 3)
            self.assertEqual(timings.connection_count, 3)

    def test_repository_transactions_remain_independent_and_failed_work_rolls_back(self) -> None:
        with retain_databases_for_read():
            with self.factory.connect(self.path) as first:
                first.begin()
                first.execute("INSERT INTO values_to_read VALUES (2)")
                self.assertEqual(self.read(), 1)
                first.commit()
            self.assertEqual(self.read(), 3)
            with self.assertRaisesRegex(ValueError, "failed work"):
                with self.factory.connect(self.path) as failed:
                    failed.begin()
                    failed.execute("INSERT INTO values_to_read VALUES (10)")
                    raise ValueError("failed work")
            self.assertEqual(self.read(), 3)

    def test_local_operation_reuses_owner_for_committed_write_and_read(self) -> None:
        with collect_duckdb_request_timings() as timings:
            with retain_databases_for_operation():
                with self.factory.connect(self.path) as connection:
                    connection.begin()
                    connection.execute("INSERT INTO values_to_read VALUES (2)")
                    connection.commit()
                self.assertEqual(self.read(), 3)

        self.assertEqual(timings.connection_count, 1)

    def test_scope_releases_every_database_even_when_rendering_fails(self) -> None:
        opened = []
        real_connect = duckdb.connect

        def connect(*args, **kwargs):
            result = real_connect(*args, **kwargs)
            opened.append(result)
            return result

        with patch("impodo.adapters.duckdb.unit_of_work.duckdb.connect", side_effect=connect):
            with self.assertRaisesRegex(ValueError, "render failed"):
                with retain_databases_for_read():
                    self.read()
                    with self.factory.connect(self.path.with_name("second.duckdb")) as other:
                        other.execute("SELECT 1")
                    raise ValueError("render failed")
            self.assertEqual(len(opened), 2)
            for connection in opened:
                with self.assertRaises(duckdb.ConnectionException):
                    connection.execute("SELECT 1")
            self.assertEqual(self.read(), 1)
            self.assertEqual(len(opened), 3)

    def test_other_threads_do_not_inherit_retained_handles(self) -> None:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with retain_databases_for_read():
                self.assertEqual(self.read(), 1)

                def other_thread():
                    with collect_duckdb_request_timings() as timings:
                        self.assertEqual(self.read(), 1)
                        self.assertEqual(self.read(), 1)
                        return timings.connection_count

                self.assertEqual(workers.submit(other_thread).result(timeout=10), 2)

    def test_another_process_can_open_the_file_after_each_read(self) -> None:
        for fail in (False, True):
            try:
                with retain_databases_for_read():
                    self.assertEqual(self.read(), 1)
                    if fail:
                        raise ValueError("render failed")
            except ValueError:
                pass
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import duckdb, sys; "
                    "c = duckdb.connect(sys.argv[1]); "
                    "assert c.execute('SELECT sum(value) FROM values_to_read').fetchone() == (1,); "
                    "c.close()"
                ), str(self.path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_overrides_are_not_silently_ignored_by_reuse(self) -> None:
        with retain_databases_for_read():
            self.assertEqual(self.read(), 1)
            with self.assertRaises(duckdb.ConnectionException):
                with self.factory.connect(self.path, threads="3"):
                    self.fail("An incompatible database configuration was reused")

    def test_schema_validation_is_reused_only_for_the_same_scoped_database(self) -> None:
        second_path = self.path.with_name("second.duckdb")

        with retain_databases_for_read():
            with self.factory.connect(self.path) as first:
                with validate_schema_once_for_operation(first, "workspace-v1") as required:
                    self.assertTrue(required)
            with self.factory.connect(self.path) as repeated:
                with validate_schema_once_for_operation(
                    repeated,
                    "workspace-v1",
                ) as required:
                    self.assertFalse(required)
                with validate_schema_once_for_operation(
                    repeated,
                    "workspace-v2",
                ) as required:
                    self.assertTrue(required)
            with self.factory.connect(second_path) as second:
                with validate_schema_once_for_operation(
                    second,
                    "workspace-v1",
                ) as required:
                    self.assertTrue(required)

        with self.factory.connect(self.path) as unscoped:
            with validate_schema_once_for_operation(
                unscoped,
                "workspace-v1",
            ) as required:
                self.assertTrue(required)

    def test_failed_schema_validation_is_not_reused(self) -> None:
        with retain_databases_for_read():
            with self.factory.connect(self.path) as failed:
                with self.assertRaisesRegex(ValueError, "invalid schema"):
                    with validate_schema_once_for_operation(
                        failed,
                        "workspace-v1",
                    ) as required:
                        self.assertTrue(required)
                        raise ValueError("invalid schema")
            with self.factory.connect(self.path) as retried:
                with validate_schema_once_for_operation(
                    retried,
                    "workspace-v1",
                ) as required:
                    self.assertTrue(required)



if __name__ == "__main__":
    unittest.main()
