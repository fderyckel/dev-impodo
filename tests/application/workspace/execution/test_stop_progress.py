"""Load workers retain durable stop authority across notification races."""

from dataclasses import replace
from threading import Event
from unittest import TestCase

from impodo.application.workspace.execution.job_models import LoadPhase
from impodo.application.workspace.execution.load_jobs import (
    LoadJobManager,
    LoadJobResult,
)
from impodo.application.workspace.execution.progress import execution_progress
from impodo.domain.execution.load_control import LoadControl, LoadControlState
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.application.workspace.execution.test_load_jobs import (
    WORKSPACE_ID,
    _access_context,
    _run,
    _wait_for_terminal,
)


def _access(recipe):
    context = _access_context()
    return (
        replace(
            context,
            recipe_application_id="50000000-0000-4000-8000-000000000001",
            run_purpose="TEST",
        )
        if recipe
        else context
    )


class StopProgressTests(TestCase):
    def test_late_worker_updates_advance_counts_without_replacing_a_pending_stop(self):
        for recipe in (False, True):
            for late_state in (None, LoadControlState.RUNNING, LoadControlState.PAUSED):
                with self.subTest(recipe=recipe, late_state=late_state):
                    manager = LoadJobManager()
                    initial_visible, late_visible, verifying_visible = (
                        Event(),
                        Event(),
                        Event(),
                    )
                    allow_late, allow_verifying, allow_finish = (
                        Event(),
                        Event(),
                        Event(),
                    )
                    template = _run(terminal=False)
                    initial = replace(
                        template,
                        rows=(
                            template.rows[0],
                            replace(
                                template.rows[1],
                                status=ExecutionRowStatus.PLANNED,
                                attempt=0,
                                odoo_id=None,
                            ),
                            replace(
                                template.rows[2],
                                status=ExecutionRowStatus.PLANNED,
                                attempt=0,
                                odoo_id=None,
                            ),
                        ),
                        load_control=LoadControl(),
                    )
                    late = replace(
                        initial,
                        rows=(initial.rows[0], template.rows[1], initial.rows[2]),
                        load_control=LoadControl(
                            state=late_state,
                            reason="CONSECUTIVE_REJECTIONS"
                            if late_state is LoadControlState.PAUSED
                            else "",
                        )
                        if late_state is not None
                        else None,
                    )
                    stopped = replace(
                        late,
                        status=ExecutionRunStatus.COMPLETED_WITH_ERRORS,
                        rows=(
                            *late.rows[:2],
                            replace(late.rows[2], status=ExecutionRowStatus.BLOCKED),
                        ),
                        load_control=LoadControl(
                            state=LoadControlState.STOPPED, reason="USER_STOP"
                        ),
                    )

                    def work(_context, writing, verifying):
                        writing(execution_progress(initial))
                        initial_visible.set()
                        self.assertTrue(allow_late.wait(5))
                        writing(execution_progress(late))
                        late_visible.set()
                        self.assertTrue(allow_verifying.wait(5))
                        verifying()
                        verifying_visible.set()
                        self.assertTrue(allow_finish.wait(5))
                        writing(execution_progress(stopped))
                        return LoadJobResult(stopped.run_id, False)

                    try:
                        queued = manager.enqueue(
                            WORKSPACE_ID,
                            "Stop progress",
                            target_database="synthetic",
                            target_server="odoo.example.test",
                            target_environment="Test",
                            total_rows=3,
                            access_context=_access(recipe),
                            actor=LOCAL_ACTOR,
                            work=work,
                        )
                        self.assertTrue(initial_visible.wait(5))
                        before = manager.get(WORKSPACE_ID, queued.job_id)
                        manager.note_stop_requested(WORKSPACE_ID, "another-run")
                        self.assertEqual(
                            manager.get(WORKSPACE_ID, queued.job_id), before
                        )
                        manager.note_stop_requested(WORKSPACE_ID, initial.run_id)
                        allow_late.set()
                        self.assertTrue(late_visible.wait(5))
                        active = manager.get(WORKSPACE_ID, queued.job_id)
                        self.assertEqual(active.load_control_state, "STOP_REQUESTED")
                        self.assertEqual(
                            active.message, "Stopping after the current request"
                        )
                        self.assertEqual(active.load_pause_reason, "")
                        self.assertEqual(
                            (
                                active.completed_rows,
                                active.created_count,
                                active.updated_count,
                            ),
                            (2, 1, 1),
                        )
                        self.assertEqual(active.unattempted_count, 1)
                        allow_verifying.set()
                        self.assertTrue(verifying_visible.wait(5))
                        verifying = manager.get(WORKSPACE_ID, queued.job_id)
                        self.assertEqual(verifying.phase, LoadPhase.VERIFYING)
                        self.assertEqual(verifying.message, active.message)
                        self.assertEqual(verifying.completed_rows, 2)
                        allow_finish.set()
                        finished = _wait_for_terminal(manager, queued.job_id)
                        self.assertEqual(finished.load_control_state, "STOPPED")
                        self.assertEqual(
                            finished.message, "Upload stopped with partial results"
                        )
                        self.assertEqual(
                            (
                                finished.completed_rows,
                                finished.blocked_count,
                                finished.unattempted_count,
                            ),
                            (3, 1, 0),
                        )
                    finally:
                        allow_late.set()
                        allow_verifying.set()
                        allow_finish.set()
                        manager.shutdown()

    def test_late_stop_notification_cannot_replace_an_observed_terminal_journal(self):
        for recipe in (False, True):
            for state in (LoadControlState.STOPPED, LoadControlState.FINISHED):
                with self.subTest(recipe=recipe, state=state):
                    manager = LoadJobManager()
                    visible, release = Event(), Event()
                    template = _run(terminal=False)
                    run = replace(
                        template,
                        status=ExecutionRunStatus.COMPLETED_WITH_ERRORS
                        if state is LoadControlState.STOPPED
                        else ExecutionRunStatus.COMPLETED,
                        rows=(
                            *template.rows[:2],
                            replace(
                                template.rows[2],
                                status=ExecutionRowStatus.BLOCKED
                                if state is LoadControlState.STOPPED
                                else ExecutionRowStatus.COMMITTED,
                                odoo_id=None
                                if state is LoadControlState.STOPPED
                                else template.rows[2].odoo_id,
                                attempt=0 if state is LoadControlState.STOPPED else 1,
                            ),
                        ),
                        load_control=LoadControl(state=state),
                    )

                    def work(_context, writing, verifying):
                        writing(execution_progress(run))
                        visible.set()
                        self.assertTrue(release.wait(5))
                        verifying()
                        return LoadJobResult(
                            run.run_id, state is LoadControlState.FINISHED
                        )

                    try:
                        queued = manager.enqueue(
                            WORKSPACE_ID,
                            "Stop progress",
                            target_database="synthetic",
                            target_server="odoo.example.test",
                            target_environment="Test",
                            total_rows=3,
                            access_context=_access(recipe),
                            actor=LOCAL_ACTOR,
                            work=work,
                        )
                        self.assertTrue(visible.wait(5))
                        manager.note_stop_requested(WORKSPACE_ID, "another-run")
                        before = manager.get(WORKSPACE_ID, queued.job_id)
                        self.assertEqual(before.load_control_state, state.value)
                        manager.note_stop_requested(WORKSPACE_ID, run.run_id)
                        after = manager.get(WORKSPACE_ID, queued.job_id)
                        self.assertEqual(after, before)
                        release.set()
                        finished = _wait_for_terminal(manager, queued.job_id)
                        self.assertEqual(finished.load_control_state, state.value)
                        self.assertEqual(
                            finished.message,
                            "Upload stopped with partial results"
                            if state is LoadControlState.STOPPED
                            else "Odoo load verified",
                        )
                    finally:
                        release.set()
                        manager.shutdown()
