"""Keep safe progress and durable recovery links for saved-data actions."""

import unittest
from datetime import UTC, datetime

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
