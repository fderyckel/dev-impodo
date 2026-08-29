"""Verify resumable, single-attempt correction background control state."""

from __future__ import annotations

from threading import Event
import time
import unittest

from impodo.application.correction_jobs import (
    CorrectionJobKind,
    CorrectionJobManager,
    CorrectionJobResult,
    CorrectionJobStatus,
)


class CorrectionJobManagerTests(unittest.TestCase):
    def test_active_review_is_reused_and_survives_progress_page_reload(self) -> None:
        manager = CorrectionJobManager()
        release = Event()

        def work(progress):
            progress(40, "Preparing corrected intent")
            release.wait(timeout=2)
            return CorrectionJobResult(field_count=3, record_count=2)

        try:
            first = manager.enqueue(
                "completed-workspace",
                "successor-workspace",
                kind=CorrectionJobKind.REVIEW,
                work=work,
            )
            replay = manager.enqueue(
                "completed-workspace",
                "successor-workspace",
                kind=CorrectionJobKind.REVIEW,
                work=work,
            )
            self.assertEqual(replay.job_id, first.job_id)
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                current = manager.get("completed-workspace", first.job_id)
                if current.terminal:
                    break
                time.sleep(0.01)
            self.assertEqual(current.status, CorrectionJobStatus.SUCCEEDED)
            self.assertEqual(current.result.record_count, 2)
            self.assertEqual(manager.latest("completed-workspace"), current)
        finally:
            manager.shutdown()

    def test_unexpected_failure_does_not_expose_its_exception_text(self) -> None:
        manager = CorrectionJobManager()

        def work(_progress):
            raise RuntimeError("protected value must not reach the browser")

        try:
            job = manager.enqueue(
                "completed-workspace",
                "successor-workspace",
                kind=CorrectionJobKind.REVIEW,
                work=work,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                current = manager.get("completed-workspace", job.job_id)
                if current.terminal:
                    break
                time.sleep(0.01)
            self.assertEqual(current.status, CorrectionJobStatus.FAILED)
            self.assertNotIn("protected value", current.failure_message)
            self.assertIn("stopped safely", current.failure_message)
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
