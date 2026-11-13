"""Keep optional numeric observations isolated from work and cleanup truth."""

from unittest import TestCase
from unittest.mock import patch

from impodo.application.shared.numeric_performance import (
    collect_numeric_validation,
    observe_numeric_failure_batch,
    observe_numeric_value_page,
    time_numeric_validation,
)


class NumericPerformanceTests(TestCase):
    """Exercise capture failures without weakening the numeric evaluator."""

    def test_unvisited_numeric_route_is_explicitly_unobserved(self):
        with collect_numeric_validation() as collector:
            pass
        self.assertEqual(collector.snapshot()["status"], "no_observations")

    def test_timer_start_failure_does_not_prevent_work(self):
        with (
            collect_numeric_validation() as collector,
            patch(
                "impodo.application.shared.numeric_performance.perf_counter",
                side_effect=OSError("optional clock unavailable"),
            ),
            time_numeric_validation(),
        ):
            result = 7

        self.assertEqual(result, 7)
        self.assertEqual(collector.snapshot()["capture_failure_count"], 1)
        self.assertEqual(collector.snapshot()["status"], "partial")

    def test_timer_end_failure_preserves_the_original_work_exception(self):
        with (
            collect_numeric_validation() as collector,
            patch(
                "impodo.application.shared.numeric_performance.perf_counter",
                side_effect=[1.0, OSError("optional clock unavailable")],
            ),
            self.assertRaisesRegex(RuntimeError, "work failed"),
            time_numeric_validation(),
        ):
            raise RuntimeError("work failed")

        self.assertEqual(collector.snapshot()["capture_failure_count"], 1)

    def test_observer_failure_is_counted_without_propagating(self):
        with (
            collect_numeric_validation() as collector,
            patch(
                "impodo.application.shared.numeric_performance.max",
                side_effect=RuntimeError("optional observation failed"),
                create=True,
            ),
        ):
            observe_numeric_value_page(cells=2, findings=1, byte_count=18)
            observe_numeric_failure_batch(rows=1, byte_count=90)

        self.assertEqual(collector.snapshot()["capture_failure_count"], 2)
        self.assertEqual(collector.snapshot()["status"], "partial")
