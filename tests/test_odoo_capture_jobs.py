from __future__ import annotations

from threading import Event
import time
from types import SimpleNamespace
import unittest

from impodo.access import LOCAL_ACTOR
from impodo.application.odoo_capture_job_service import OdooCaptureJobManager
from impodo.domain.odoo_source_capture import OdooSourceCaptureCancelled
from impodo.odoo_capture_jobs import (
    OdooCaptureJobStatus,
    OdooCapturePhase,
    OdooCaptureProgress,
)


class OdooCaptureJobManagerTests(unittest.TestCase):
    def tearDown(self) -> None:
        self.manager.shutdown()

    def test_reports_stream_counters_and_published_manifest(self) -> None:
        publication = _Publication()
        self.manager = OdooCaptureJobManager(publication)

        job = self.manager.enqueue(
            "project-1",
            "Contacts",
            1_000,
            object(),
            actor=LOCAL_ACTOR,
        )
        terminal = _wait_for_terminal(self.manager, job.project_id, job.job_id)

        self.assertEqual(terminal.status, OdooCaptureJobStatus.SUCCEEDED)
        self.assertEqual(terminal.completed_rows, 2)
        self.assertEqual(terminal.page_count, 1)
        self.assertEqual(terminal.response_bytes, 102)
        self.assertEqual(terminal.normalized_bytes, 20)
        self.assertEqual(terminal.manifest_id, "manifest-1")
        self.assertEqual(terminal.progress_percent, 100)

    def test_returns_one_active_attempt_and_cancels_at_checkpoint(self) -> None:
        publication = _BlockingPublication()
        self.manager = OdooCaptureJobManager(publication)
        first = self.manager.enqueue(
            "project-1",
            "Contacts",
            1_000,
            object(),
            actor=LOCAL_ACTOR,
        )
        duplicate = self.manager.enqueue(
            "project-1",
            "Contacts",
            1_000,
            object(),
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(duplicate.job_id, first.job_id)
        self.assertTrue(publication.started.wait(timeout=1))

        self.manager.cancel(first.project_id, first.job_id)
        terminal = _wait_for_terminal(self.manager, first.project_id, first.job_id)

        self.assertEqual(terminal.status, OdooCaptureJobStatus.CANCELLED)
        self.assertTrue(terminal.cancel_requested)


class _Publication:
    def publish(self, project_id, gateway, *, actor, cancellation, progress):
        progress(
            OdooCaptureProgress(
                phase=OdooCapturePhase.READING,
                completed_rows=2,
                total_rows=1_000,
                page_count=1,
                response_bytes=100,
                normalized_bytes=20,
            )
        )
        progress(
            OdooCaptureProgress(
                phase=OdooCapturePhase.PUBLISHING,
                completed_rows=2,
                total_rows=1_000,
                page_count=1,
                response_bytes=102,
                normalized_bytes=20,
            )
        )
        return SimpleNamespace(manifest=SimpleNamespace(manifest_id="manifest-1"))


class _BlockingPublication:
    def __init__(self) -> None:
        self.started = Event()

    def publish(self, project_id, gateway, *, actor, cancellation, progress):
        self.started.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if cancellation():
                raise OdooSourceCaptureCancelled("cancelled")
            time.sleep(0.005)
        raise AssertionError("Expected cancellation")


def _wait_for_terminal(manager, project_id: str, job_id: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = manager.get(project_id, job_id)
        if job.terminal:
            return job
        time.sleep(0.01)
    raise AssertionError("Odoo capture job did not finish")


if __name__ == "__main__":
    unittest.main()
