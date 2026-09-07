from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.domain.workspace.errors import WorkspaceDatabaseBusyError


LOCK_ERROR = (
    'IO Error: Cannot open file "workspace-engine.duckdb": The process cannot access '
    "the file because it is being used by another process."
)


class DuckDbLockContentionTests(unittest.TestCase):
    def test_preparation_connection_waits_for_transient_process_lock(self) -> None:
        connection = MagicMock(spec=duckdb.DuckDBPyConnection)
        factory = DuckDbConnectionFactory(
            lock_wait_timeout_seconds=1.0,
            lock_retry_interval_seconds=0.001,
        )

        with collect_duckdb_request_timings() as timings:
            with patch(
                "impodo.adapters.duckdb.unit_of_work.duckdb.connect",
                side_effect=(duckdb.IOException(LOCK_ERROR), connection),
            ) as connect:
                with factory.connect(Path("workspace-engine.duckdb")) as opened:
                    self.assertIs(opened, connection)

        self.assertEqual(connect.call_count, 2)
        connection.close.assert_called_once_with()
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.lock_retry_count, 1)
        self.assertGreater(timings.connect_ms, 0)
        self.assertGreaterEqual(timings.lock_wait_ms, 1.0)

    def test_process_lock_is_reported_as_a_recoverable_workspace_failure(
        self,
    ) -> None:
        factory = DuckDbConnectionFactory()

        with (
            patch(
                "impodo.adapters.duckdb.unit_of_work.duckdb.connect",
                side_effect=duckdb.IOException(LOCK_ERROR),
            ),
            self.assertRaises(WorkspaceDatabaseBusyError) as raised,
        ):
            with factory.connect(Path("workspace-engine.duckdb")):
                pass

        self.assertEqual(
            raised.exception.failure_code,
            "WORKSPACE_DATABASE_BUSY",
        )
        self.assertIn("Another Impodo task", str(raised.exception))
        self.assertIn("No Odoo records were changed", str(raised.exception))

    def test_unrelated_io_error_is_not_mislabeled_as_lock_contention(self) -> None:
        factory = DuckDbConnectionFactory(lock_wait_timeout_seconds=1.0)
        error = duckdb.IOException("IO Error: No space left on device")

        with (
            patch(
                "impodo.adapters.duckdb.unit_of_work.duckdb.connect",
                side_effect=error,
            ),
            self.assertRaises(duckdb.IOException) as raised,
        ):
            with factory.connect(Path("workspace-engine.duckdb")):
                pass

        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
