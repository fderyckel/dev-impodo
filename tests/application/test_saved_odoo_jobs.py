"""Keep safe progress and durable recovery links for saved-data actions."""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from impodo.application.odoo_saved_data_jobs import (
    SavedOdooActionError,
    SavedOdooDataJobManager,
    SavedOdooJob,
)


class SavedOdooJobTests(unittest.TestCase):
    def setUp(self):
        self.manager = SavedOdooDataJobManager()
        self.addCleanup(self.manager.shutdown)
        self.job = SavedOdooJob(
            "job",
            "project",
            "saved-data",
            "RESTORE",
            "QUEUED",
            0,
            "Waiting",
            "",
            datetime.now(UTC),
        )
        self.manager._jobs[self.job.job_id] = self.job

    def test_stopped_action_keeps_exact_safe_recovery_link(self):
        def work(progress):
            progress(70, "Checking committed records")
            raise SavedOdooActionError(
                "Review the interrupted restore",
                redirect_url="/workspaces/restore/transfer-load/outcome",
            )

        self.manager._execute(self.job.job_id, work)
        result = self.manager.get("project", "saved-data", "job")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.progress_percent, 70)
        self.assertEqual(
            result.redirect_url, "/workspaces/restore/transfer-load/outcome"
        )
        self.assertEqual(result.failure_message, "Review the interrupted restore")

    def test_unexpected_error_never_exposes_credential_or_raw_response(self):
        def work(_progress):
            raise RuntimeError("Authorization: destination-only-secret")

        self.manager._execute(self.job.job_id, work)
        result = self.manager.get("project", "saved-data", "job")
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("destination-only-secret", result.failure_message)
        self.assertNotIn("Authorization", result.failure_message)

    def test_progress_lookup_cannot_cross_project_or_saved_configuration(self):
        for project, saved in (
            ("another-project", "saved-data"),
            ("project", "another-saved"),
        ):
            with (
                self.subTest(project=project, saved=saved),
                self.assertRaises(LookupError),
            ):
                self.manager.get(project, saved, "job")

    def test_recovery_link_cannot_redirect_to_external_site(self):
        for location in ("https://external.example", "//external.example"):
            with self.subTest(location=location), self.assertRaises(ValueError):
                SavedOdooActionError("Stopped", redirect_url=location)

    def test_project_progress_tracks_queued_jobs_without_resuming_terminal_work(self):
        def capture(_progress):
            return "/capture/result"

        def restore(_progress):
            raise SavedOdooActionError("Review the restore")

        with patch.object(self.manager._runner, "submit"):
            first = self.manager.enqueue("one", "set-a", kind="UPDATE", work=capture)
            second = self.manager.enqueue("one", "set-b", kind="RESTORE", work=restore)
            other = self.manager.enqueue("two", "set-c", kind="UPDATE", work=capture)
            self.assertEqual(
                self.manager.enqueue("one", "set-a", kind="UPDATE", work=capture),
                first,
            )
        self.assertEqual(self.manager.active_for_project("one"), first)
        self.assertEqual(self.manager.active_for_project("two"), other)
        self.assertIsNone(self.manager.active_for_project("another-project"))
        self.manager._execute(first.job_id, capture)
        self.assertEqual(self.manager.active_for_project("one"), second)
        self.manager._execute(second.job_id, restore)
        self.assertIsNone(self.manager.active_for_project("one"))
        self.assertEqual(self.manager.active_for_project("two"), other)
        self.assertEqual(self.manager.get("one", "set-b", second.job_id).status, "FAILED")

    def test_rejected_submission_does_not_leave_project_busy(self):
        with (
            patch.object(self.manager._runner, "submit", side_effect=RuntimeError("stopped")),
            self.assertRaises(RuntimeError),
        ):
            self.manager.enqueue("one", "set-a", kind="UPDATE", work=lambda _: "/result")
        self.assertIsNone(self.manager.active_for_project("one"))
