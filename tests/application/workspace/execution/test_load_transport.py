"""Confirmed rollback permits splitting; unknown receipts never permit resend."""

import unittest
from dataclasses import replace

from impodo.application.workspace.execution.load_transport import (
    create_transport_results,
)
from impodo.domain.execution.load_control import LoadHalted
from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from tests.application.workspace.execution.test_service import _Executor, _snapshot


class _RejectingExecutor(_Executor):
    def __init__(self, *, unknown=False):
        super().__init__("scope")
        self.calls = []
        self.committed = []
        self.unknown_child = unknown

    def load_create_rows(self, model, values, external_ids):
        self.calls.append(tuple(external_ids))
        if self.unknown_child and len(self.calls) == 2:
            raise OdooWriteOutcomeUnknown("lost receipt")
        if any(item["name"] == "bad" for item in values):
            raise OdooWriteRejected(
                "safe validation",
                diagnostic=OdooWriteDiagnostic(
                    WriteFailureCode.VALIDATION, model, "load", batch_size=len(values)
                ),
            )
        self.committed.extend(external_ids)
        return tuple(
            range(len(self.committed) - len(values) + 1, len(self.committed) + 1)
        )


class LoadTransportTests(unittest.TestCase):
    def _prepared(self):
        row = _snapshot().rows[0]
        return tuple(
            (
                replace(row, proposed_external_id=f"impodo_test.r{i}"),
                {"name": "bad" if i == 2 else str(i)},
                (),
            )
            for i in range(5)
        )

    def test_isolates_bad_row_and_commits_each_innocent_row_once(self):
        executor = _RejectingExecutor()
        starts, rollbacks = [], []
        results = list(
            create_transport_results(
                self._prepared(),
                model="res.partner",
                executor=executor,
                import_create=True,
                continuation=True,
                begin=starts.append,
                rolled_back=lambda rows, error: rollbacks.append(rows),
            )
        )
        self.assertEqual(len(executor.committed), 4)
        self.assertEqual(len(set(executor.committed)), 4)
        failures = [item for item in results if item.error]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].prepared[0][1]["name"], "bad")
        self.assertEqual(len(starts), len(executor.calls))
        self.assertEqual(len(rollbacks), 2)

    def test_unknown_child_stops_without_another_call(self):
        executor = _RejectingExecutor(unknown=True)
        results = list(
            create_transport_results(
                self._prepared(),
                model="res.partner",
                executor=executor,
                import_create=True,
                continuation=True,
                begin=lambda rows: None,
                rolled_back=lambda rows, error: None,
            )
        )
        self.assertIsInstance(results[-1].error, OdooWriteOutcomeUnknown)
        self.assertEqual(len(executor.calls), 2)

    def test_stop_between_split_attempts_leaves_peers_unattempted(self):
        executor = _RejectingExecutor()

        def begin(rows):
            if executor.calls:
                raise LoadHalted("USER_STOP")

        with self.assertRaises(LoadHalted):
            list(
                create_transport_results(
                    self._prepared(),
                    model="res.partner",
                    executor=executor,
                    import_create=True,
                    continuation=True,
                    begin=begin,
                    rolled_back=lambda rows, error: None,
                )
            )
        self.assertEqual(len(executor.calls), 1)

    def test_legacy_policy_never_splits(self):
        executor = _RejectingExecutor()
        results = list(
            create_transport_results(
                self._prepared(),
                model="res.partner",
                executor=executor,
                import_create=True,
                continuation=False,
                begin=lambda rows: None,
                rolled_back=lambda rows, error: self.fail("legacy run split"),
            )
        )
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(len(results[0].prepared), 5)
