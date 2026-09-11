"""Verify the shared serial background task execution primitive."""

from __future__ import annotations

from threading import Event, Lock
from unittest import TestCase

from impodo.application.shared.serial_job_runner import (
    SerialJobRunner,
    SerialJobRunnerStoppedError,
)


class SerialJobRunnerTests(TestCase):
    def setUp(self) -> None:
        self.runner = SerialJobRunner(worker_name="impodo-test-serial-jobs")
        self.addCleanup(self.runner.shutdown)

    def test_runs_tasks_one_at_a_time_in_submission_order(self) -> None:
        first_started = Event()
        release_first = Event()
        second_finished = Event()
        order: list[str] = []
        order_lock = Lock()

        def first() -> None:
            with order_lock:
                order.append("first-started")
            first_started.set()
            self.assertTrue(release_first.wait(timeout=2))
            with order_lock:
                order.append("first-finished")

        def second() -> None:
            with order_lock:
                order.append("second")
            second_finished.set()

        self.runner.submit("first", first, discard=lambda: None)
        self.runner.submit("second", second, discard=lambda: None)

        self.assertTrue(first_started.wait(timeout=2))
        with order_lock:
            self.assertEqual(order, ["first-started"])
        release_first.set()
        self.assertTrue(second_finished.wait(timeout=2))
        with order_lock:
            self.assertEqual(
                order,
                ["first-started", "first-finished", "second"],
            )

    def test_unhandled_task_failure_does_not_strand_the_next_task(self) -> None:
        next_task_finished = Event()

        def fail() -> None:
            raise RuntimeError("test task failure")

        with self.assertLogs(
            "impodo.application.shared.serial_job_runner",
            level="ERROR",
        ):
            self.runner.submit("failing", fail, discard=lambda: None)
            self.runner.submit(
                "next",
                next_task_finished.set,
                discard=lambda: None,
            )
            self.assertTrue(next_task_finished.wait(timeout=2))

    def test_shutdown_discards_queued_work_and_rejects_new_work(self) -> None:
        active_started = Event()
        release_active = Event()
        active_finished = Event()
        queued_executed = Event()
        queued_discarded = Event()

        def active() -> None:
            active_started.set()
            release_active.wait(timeout=2)
            active_finished.set()

        self.runner.submit("active", active, discard=lambda: None)
        self.runner.submit(
            "queued",
            queued_executed.set,
            discard=queued_discarded.set,
        )
        self.assertTrue(active_started.wait(timeout=2))

        self.runner.shutdown()

        self.assertTrue(queued_discarded.is_set())
        self.assertFalse(queued_executed.is_set())
        self.assertFalse(active_finished.is_set())
        with self.assertRaises(SerialJobRunnerStoppedError):
            self.runner.submit("late", lambda: None, discard=lambda: None)

        release_active.set()
        self.assertTrue(active_finished.wait(timeout=2))
