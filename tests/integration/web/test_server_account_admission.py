"""Verify bounded account admission under concurrent calls and clock advancement."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from impodo.web.composition.server_account_admission import (
    ServerAccountAdmission,
    ServerAccountBusyError,
)


class ServerAccountAdmissionTests(unittest.TestCase):
    def test_concurrent_work_rejects_immediately_and_releases_after_failure(self):
        gate = ServerAccountAdmission(concurrency=1)
        entered, release = Event(), Event()

        def work():
            with gate.admit("source-one"):
                entered.set()
                self.assertTrue(release.wait(5))
                raise ValueError("fictional operation failure")

        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(work)
            try:
                self.assertTrue(entered.wait(5))
                with (
                    self.assertRaises(ServerAccountBusyError),
                    gate.admit("source-two"),
                ):
                    self.fail("Capacity was exceeded")
            finally:
                release.set()
            with self.assertRaises(ValueError):
                future.result(5)
        with gate.admit("source-two"):
            pass

    def test_rates_recover_without_sleeping_and_source_state_has_a_ceiling(self):
        now = [0.0]
        gate = ServerAccountAdmission(
            global_burst=3, source_burst=1, max_sources=2, clock=lambda: now[0]
        )
        with gate.admit("first"):
            pass
        with self.assertRaises(ServerAccountBusyError), gate.admit("first"):
            self.fail("Source rate exceeded")
        with gate.admit("second"):
            pass
        with self.assertRaises(ServerAccountBusyError), gate.admit("third"):
            self.fail("Source bookkeeping exceeded its bound")
        now[0] = 60.0
        with gate.admit("third"):
            pass

    def test_global_budget_applies_across_distinct_sources(self):
        gate = ServerAccountAdmission(global_burst=1)
        with gate.admit("first"):
            pass
        with self.assertRaises(ServerAccountBusyError), gate.admit("second"):
            self.fail("Global rate exceeded")
