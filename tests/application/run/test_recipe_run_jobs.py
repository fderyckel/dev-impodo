"""Verify resumable Recipe-run progress job supervision."""

from __future__ import annotations

from threading import Event
from time import monotonic, sleep
from unittest import TestCase

from impodo.application.run.recipe_run_jobs import (
    RecipeRunJobKind,
    RecipeRunJobManager,
    RecipeRunJobPhase,
    RecipeRunJobProgress,
    RecipeRunJobResult,
    RecipeRunJobStatus,
)


class RecipeRunJobManagerTests(TestCase):
    def setUp(self) -> None:
        self.manager = RecipeRunJobManager()
        self.addCleanup(self.manager.shutdown)

    def test_reports_progress_deduplicates_active_work_and_finishes(self) -> None:
        checkpoint = Event()
        release = Event()

        def work(report):
            report(
                RecipeRunJobProgress(
                    RecipeRunJobPhase.READING_ODOO,
                    "Reading Contacts from Odoo",
                    30,
                    completed_units=2,
                    total_units=5,
                    unit_label="models",
                )
            )
            checkpoint.set()
            self.assertTrue(release.wait(timeout=2))
            return RecipeRunJobResult("/next", "Odoo is ready")

        job = self.manager.enqueue(
            kind=RecipeRunJobKind.ODOO_CHECK,
            project_id="project-1",
            migration_run_id="run-1",
            workspace_id="workspace-1",
            work=work,
        )
        self.assertTrue(checkpoint.wait(timeout=2))

        running = self.manager.get("project-1", "run-1", job.job_id)
        self.assertEqual(running.status, RecipeRunJobStatus.RUNNING)
        self.assertEqual(running.phase, RecipeRunJobPhase.READING_ODOO)
        self.assertEqual(running.progress_percent, 30)
        self.assertEqual((running.completed_units, running.total_units), (2, 5))

        duplicate = self.manager.enqueue(
            kind=RecipeRunJobKind.ODOO_CHECK,
            project_id="project-1",
            migration_run_id="run-1",
            workspace_id="workspace-1",
            work=lambda _report: RecipeRunJobResult("/wrong", "Wrong work"),
        )
        self.assertEqual(duplicate.job_id, job.job_id)

        release.set()
        finished = self._wait_for_terminal(job.job_id)
        self.assertEqual(finished.status, RecipeRunJobStatus.SUCCEEDED)
        self.assertEqual(finished.progress_percent, 100)
        self.assertEqual(finished.redirect_url, "/next")
        self.assertEqual(finished.message, "Odoo is ready")

    def test_expected_failures_are_saved_without_losing_the_current_phase(self) -> None:
        def work(report):
            report(
                RecipeRunJobProgress(
                    RecipeRunJobPhase.TARGET_MATCHES,
                    "Comparing target values",
                    40,
                )
            )
            raise _ExpectedRecipeError("The saved target evidence changed")

        job = self.manager.enqueue(
            kind=RecipeRunJobKind.TARGET_MATCH_REVIEW,
            project_id="project-1",
            migration_run_id="run-1",
            workspace_id="workspace-1",
            application_id="application-1",
            work=work,
        )

        finished = self._wait_for_terminal(job.job_id)
        self.assertEqual(finished.status, RecipeRunJobStatus.FAILED)
        self.assertEqual(finished.phase, RecipeRunJobPhase.TARGET_MATCHES)
        self.assertEqual(finished.progress_percent, 40)
        self.assertIn("could not finish", finished.failure_message)

    def _wait_for_terminal(self, job_id: str):
        deadline = monotonic() + 3
        while monotonic() < deadline:
            job = self.manager.get("project-1", "run-1", job_id)
            if job.terminal:
                return job
            sleep(0.01)
        self.fail("Recipe run job did not finish")


class _ExpectedRecipeError(RuntimeError):
    pass
