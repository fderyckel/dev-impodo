"""Show bounded Odoo waits for Authoring and Recipe load workers."""

import unittest
from dataclasses import replace
from threading import Event

from impodo.application.shared.remote_wait import report_remote_wait
from impodo.application.workspace.execution.load_jobs import (
    LoadJobManager,
    LoadJobResult,
)
from impodo.application.workspace.execution.progress import execution_progress
from impodo.domain.execution.load_control import LoadHalted
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.application.workspace.execution.test_load_jobs import (
    WORKSPACE_ID,
    _access_context,
    _run,
    _wait_for_terminal,
)


class RequestWaitProgressTests(unittest.TestCase):
    def test_wait_is_visible_and_stop_prevents_the_next_send(self):
        for recipe in (False, True):
            with self.subTest(recipe=recipe):
                waiting, continue_worker = Event(), Event()
                manager = LoadJobManager()
                access = _access_context()
                if recipe:
                    access = replace(
                        access,
                        recipe_application_id="50000000-0000-4000-8000-000000000001",
                        run_purpose="TEST",
                    )
                run = _run(terminal=False)
                stopped = []

                def work(_access, writing, _verifying):
                    writing(execution_progress(run))
                    report_remote_wait(10.0)
                    waiting.set()
                    self.assertTrue(continue_worker.wait(2))
                    try:
                        report_remote_wait(9.0)
                    except LoadHalted:
                        stopped.append(True)
                    finally:
                        report_remote_wait(None)
                    return LoadJobResult(run.run_id, False)

                try:
                    queued = manager.enqueue(
                        WORKSPACE_ID,
                        "Synthetic",
                        target_database="synthetic",
                        target_server="odoo.example.test",
                        target_environment="Test",
                        total_rows=3,
                        access_context=access,
                        actor=LOCAL_ACTOR,
                        work=work,
                    )
                    self.assertTrue(waiting.wait(2))
                    job = manager.get(WORKSPACE_ID, queued.job_id)
                    self.assertIn("next check in 10 seconds", job.message)
                    self.assertEqual(job.completed_rows, 2)
                    manager.note_stop_requested(WORKSPACE_ID, run.run_id)
                    continue_worker.set()
                    _wait_for_terminal(manager, queued.job_id)
                    self.assertEqual(stopped, [True])
                    self.assertEqual(manager._wait_started, {})
                finally:
                    continue_worker.set()
                    manager.shutdown()
