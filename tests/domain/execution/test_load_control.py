"""Failure budgets count settled rows rather than rolled-back peers."""

import json
import unittest
from dataclasses import replace

from impodo.domain.execution.load_control import (
    LoadControl,
    LoadControlState,
    LoadHalted,
    RejectionPolicy,
    can_set_aside,
)
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)


class LoadControlTests(unittest.TestCase):
    def test_all_fail_pauses_after_ten_of_twenty_thousand(self):
        control = LoadControl(policy=RejectionPolicy.CONTINUE)
        for _ in range(10):
            control.require_running()
            control = control.settle("res.partner", "CREATE", rejected=True)
        self.assertEqual(control.reason, "CONSECUTIVE_REJECTIONS")
        self.assertEqual(len(control.windows[0].recent), 10)
        with self.assertRaises(LoadHalted):
            control.require_running()
        self.assertEqual(LoadControl.from_json(control.to_json()), control)

    def test_rate_uses_distinct_results_per_model_and_operation(self):
        control = LoadControl(policy=RejectionPolicy.CONTINUE)
        for index in range(20):
            control = control.settle("res.partner", "CREATE", rejected=index % 5 == 0)
            control = control.settle("other.model", "CREATE", rejected=False)
        self.assertEqual(control.reason, "REJECTION_RATE")
        self.assertEqual(control.state, LoadControlState.PAUSED)
        self.assertEqual(control.continue_next_group().windows, ())

    def test_stop_is_durable_and_cannot_be_resumed_as_a_pause(self):
        control = LoadControl(policy=RejectionPolicy.CONTINUE).request_stop()
        self.assertEqual(control, control.settle("m", "CREATE", rejected=False))
        with self.assertRaises(ValueError):
            control.continue_next_group()
        with self.assertRaises(LoadHalted):
            LoadControl.from_json(control.to_json()).require_running()

    def test_isolation_budget_and_success_reset(self):
        control = LoadControl(policy=RejectionPolicy.CONTINUE)
        for _ in range(24):
            control = control.isolate()
        self.assertEqual(
            control.settle("m", "CREATE", rejected=False).isolation_calls, 0
        )
        at_limit = control.isolate()
        self.assertEqual(at_limit.settle_many(()).reason, "ISOLATION_BUDGET")
        successful = at_limit.settle("m", "CREATE", rejected=False)
        self.assertEqual(successful.state, LoadControlState.RUNNING)
        self.assertEqual(successful.isolation_calls, 0)

    def test_legacy_stop_policy_and_unknown_errors_do_not_continue(self):
        control = LoadControl()
        for _ in range(12):
            control = control.settle("m", "CREATE", rejected=True)
        self.assertEqual(control.state, LoadControlState.RUNNING)
        for code in WriteFailureCode:
            error = OdooWriteRejected(
                "safe", diagnostic=OdooWriteDiagnostic(code, "res.partner", "load")
            )
            self.assertEqual(
                can_set_aside(error),
                code
                not in {
                    WriteFailureCode.ACCESS_DENIED,
                    WriteFailureCode.REJECTED,
                    WriteFailureCode.UNKNOWN,
                },
            )

    def test_changed_evidence_is_rejected(self):
        payload = json.loads(LoadControl().to_json())
        payload["policy"] = "CONTINUE"
        with self.assertRaises(ValueError):
            LoadControl.from_json(json.dumps(payload))
        with self.assertRaises(ValueError):
            replace(LoadControl(), windows=(), isolation_limit=0)

    def test_success_receipt_is_counted_completely_before_rate_is_evaluated(self):
        control = LoadControl(policy=RejectionPolicy.CONTINUE)
        for index in range(19):
            control = control.settle("res.partner", "CREATE", rejected=index % 5 == 0)
        control = control.settle_many(
            ("res.partner", "CREATE", False) for _ in range(100)
        )
        self.assertEqual(control.state, LoadControlState.RUNNING)
        self.assertEqual(control.windows[0].recent, (False,) * 50)

    def test_large_strict_rejection_receipt_remains_bounded_and_recordable(self):
        control = LoadControl().settle_many(
            ("res.partner", "CREATE", True) for _ in range(1_000)
        )
        self.assertEqual(control.windows[0].consecutive, 1_000)
        self.assertEqual(len(control.windows[0].recent), 50)
