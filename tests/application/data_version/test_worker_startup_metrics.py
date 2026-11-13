"""Preserve source-worker deadlines and results while measuring startup."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from impodo.application.data_version.source_worker import (
    _receive_source_result,
    _start_source_process,
    _worker,
)
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.shared.worker_startup import (
    collect_worker_startup,
    record_worker_startup,
)
from impodo.domain.preparation.source import SourceLoadError

MODULE = "impodo.application.data_version.source_worker"


class SourceWorkerStartupMetricsTests(unittest.TestCase):
    def test_readiness_does_not_reset_the_operation_timeout(self) -> None:
        receiver, process = MagicMock(), MagicMock()
        receiver.recv.side_effect = [
            ("startup", {"build_hash_ms": 12.0, "build_hash_files": 3}),
            ("ok", "accepted"),
        ]
        with (
            collect_worker_startup() as observation,
            patch(f"{MODULE}.time.monotonic", side_effect=[0.0, 0.0, 12.0]),
            patch(f"{MODULE}._observation_clock", side_effect=[2.0, 5.0]),
            patch(f"{MODULE}._poll_with_memory_limit", return_value=True) as poll,
        ):
            result = _receive_source_result(
                receiver,
                process,
                timeout_seconds=30,
                operation="validation",
                error_type=SourceLoadError,
                start_finished=1.0,
            )
        self.assertEqual(result, ("ok", "accepted"))
        self.assertEqual(
            [call.kwargs["timeout_seconds"] for call in poll.call_args_list],
            [30.0, 18.0],
        )
        facts = observation.snapshot()["operations"]["validation"]
        self.assertEqual(facts["first_message_wait_ms"], 1000)
        self.assertEqual(facts["result_wait_ms"], 3000)
        self.assertEqual(facts["build_hash_ms"], 12)
        self.assertEqual(facts["readiness_count"], 1)
        self.assertEqual(facts["result_count"], 1)

    def test_timeout_after_readiness_stays_a_timeout(self) -> None:
        receiver, process = MagicMock(), MagicMock()
        receiver.recv.return_value = ("startup", {})
        with (
            collect_worker_startup() as observation,
            patch(f"{MODULE}.time.monotonic", side_effect=[0.0, 0.0, 29.0]),
            patch(f"{MODULE}._poll_with_memory_limit", side_effect=[True, False]),
        ):
            result = _receive_source_result(
                receiver,
                process,
                timeout_seconds=30,
                operation="inspection",
                error_type=SourceLoadError,
                start_finished=None,
            )
        self.assertIsNone(result)
        facts = observation.snapshot()["operations"]["inspection"]
        self.assertEqual(facts["readiness_count"], 1)
        self.assertNotIn("result_count", facts)
        self.assertNotIn("result_wait_ms", facts)

    def test_optional_missing_readiness_preserves_the_terminal_result(self) -> None:
        receiver, process = MagicMock(), MagicMock()
        receiver.recv.return_value = ("ok", "catalog-json")
        with (
            collect_worker_startup() as observation,
            patch(f"{MODULE}._poll_with_memory_limit", return_value=True),
        ):
            result = _receive_source_result(
                receiver,
                process,
                timeout_seconds=30,
                operation="inspection",
                error_type=SourceLoadError,
                start_finished=0.0,
            )
        self.assertEqual(result, ("ok", "catalog-json"))
        facts = observation.snapshot()["operations"]["inspection"]
        self.assertEqual(facts["result_count"], 1)
        self.assertNotIn("first_message_wait_ms", facts)
        self.assertNotIn("result_wait_ms", facts)

    def test_start_failure_is_measured_and_the_same_exception_escapes(self) -> None:
        failure = OSError("process creation failed")
        process = MagicMock()
        process.start.side_effect = failure
        with (
            collect_worker_startup() as observation,
            patch(f"{MODULE}._observation_clock", side_effect=[1.0, 1.25]),
        ):
            with self.assertRaises(OSError) as raised:
                _start_source_process(process, "validation")
        self.assertIs(raised.exception, failure)
        facts = observation.snapshot()["operations"]["validation"]
        self.assertEqual(facts["worker_count"], 1)
        self.assertEqual(facts["start_failure_count"], 1)
        self.assertEqual(facts["start_call_ms"], 250)

    def test_missing_measurement_clock_cannot_stop_process_creation(self) -> None:
        process = MagicMock()
        with (
            collect_worker_startup() as observation,
            patch(
                f"{MODULE}.time.perf_counter", side_effect=RuntimeError("unavailable")
            ),
        ):
            self.assertIsNone(_start_source_process(process, "validation"))
        process.start.assert_called_once_with()
        facts = observation.snapshot()["operations"]["validation"]
        self.assertEqual(facts, {"worker_count": 1})

    def test_child_control_message_carries_no_source_values_and_result_is_unchanged(
        self,
    ) -> None:
        sender, start_event = MagicMock(), MagicMock()
        with (
            patch(f"{MODULE}._limit_unix_memory"),
            patch(f"{MODULE}.validate_source_file") as validate,
        ):
            _worker("private-source.csv", PROCESS_BUILD_CONTRACT, sender, start_event)
        messages = [call.args[0] for call in sender.send.call_args_list]
        self.assertEqual(messages[0][0], "startup")
        self.assertEqual(messages[-1], ("ok", ""))
        self.assertNotIn("private-source", str(messages))
        self.assertNotIn("application_build_id", str(messages))
        validate.assert_called_once_with("private-source.csv")
        sender.close.assert_called_once_with()

    def test_observation_failure_is_isolated_from_the_source_result(self) -> None:
        with (
            collect_worker_startup() as observation,
            patch.object(
                observation, "record", side_effect=RuntimeError("unavailable")
            ),
        ):
            record_worker_startup("validation", worker_count=1)
        self.assertEqual(observation.snapshot()["operations"], {})


if __name__ == "__main__":
    unittest.main()
