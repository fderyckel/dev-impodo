"""Exercise worker exit, restoration, and bounded run recovery decisions."""

from dataclasses import replace
from queue import Empty
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.preparation.job_models import PreparationJobStatus, PreparationWorkspace
from impodo.application.workspace.preparation.recovery import RecoveredPreparation
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.web.composition.preparation_job_manager import PreparationJobManager
from impodo.web.run_commands import enqueue_preparation, recover_run_preparation
from impodo.application.workspace.preparation.preparation_job_registry import PreparationJobNotFoundError


class PreparationResultRecoveryTests(TestCase):
    def setUp(self):
        self.workspace = PreparationWorkspace(
            project_id=str(uuid4()), data_version_id=str(uuid4()), data_version_number=1,
            data_version_purpose="TEST", migration_run_id=str(uuid4()), migration_run_purpose="TEST",
            workspace_id=str(uuid4()), recipe_application_id=str(uuid4()), mapping_content_hash=content_hash("mapping"),
        )
        self.result = RecoveredPreparation(self.workspace.mapping_content_hash, str(uuid4()), str(uuid4()), PreparationJobStatus.SUCCEEDED)
        self.manager = PreparationJobManager(".tmp/recovery-unit")
        self.listener = Mock()
        self.manager.set_status_listener(self.listener)

    def job(self):
        job, _ = self.manager.registry.enqueue(
            self.workspace.workspace_id, "Customers", 10, LOCAL_ACTOR.identity, self.workspace, PROCESS_BUILD_CONTRACT,
        )
        self.manager.registry.mark_running(job.job_id)
        return job

    def exited_worker(self, job, *, terminal=None):
        process = Mock(is_alive=Mock(return_value=False))
        events = Mock(get_nowait=Mock(side_effect=[terminal, Empty()] if terminal else Empty()))
        self.manager._supervise(job.job_id, process, events)
        process.join.assert_called_once()
        events.close.assert_called_once()
        return self.manager.get(self.workspace.workspace_id, job.job_id)

    def test_lost_final_message_restores_complete_or_duplicate_review_result(self):
        for status in (PreparationJobStatus.SUCCEEDED, PreparationJobStatus.REVIEW_REQUIRED):
            with self.subTest(status=status):
                result = replace(self.result, status=status)
                reader = Mock(return_value=result)
                self.manager.set_result_reader(reader)
                job = self.job()
                recovered = self.exited_worker(job)
                self.assertEqual(recovered.status, status)
                self.assertEqual(recovered.result_run_id, result.result_run_id)
                reader.assert_called_once()

    def test_absent_or_incomplete_evidence_keeps_worker_failure_retryable(self):
        self.manager.set_result_reader(Mock(return_value=None))
        recovered = self.exited_worker(self.job())
        self.assertEqual(recovered.failure_code, "WORKER_EXITED")
        self.assertTrue(recovered.retry_allowed)
        self.assertFalse(self.manager._workers)

    def test_read_failure_does_not_claim_completion_or_hold_worker_slot(self):
        self.manager.set_result_reader(Mock(side_effect=RuntimeError("Storage unavailable")))
        with self.assertLogs("impodo.web.composition.preparation_job_manager", level="ERROR"):
            recovered = self.exited_worker(self.job())
        self.assertEqual(recovered.status, PreparationJobStatus.FAILED)
        self.assertFalse(self.manager._workers)

    def test_an_explicit_failure_or_cancellation_is_not_overridden(self):
        reader = Mock(return_value=self.result)
        self.manager.set_result_reader(reader)
        for event, status in ((('failed', 'BAD_SOURCE', 'Review source'), PreparationJobStatus.FAILED),
                              (('cancelled',), PreparationJobStatus.CANCELLED)):
            with self.subTest(status=status):
                recovered = self.exited_worker(self.job(), terminal=event)
                self.assertEqual(recovered.status, status)
        reader.assert_not_called()

    def test_wrong_mapping_result_leaves_worker_failure_retryable(self):
        self.manager.set_result_reader(Mock(return_value=replace(self.result, mapping_content_hash=content_hash("older"))))
        with self.assertLogs("impodo.web.composition.preparation_job_manager", level="ERROR"):
            recovered = self.exited_worker(self.job())
        self.assertEqual(recovered.failure_code, "WORKER_EXITED")
        self.assertTrue(recovered.retry_allowed)

    def test_restoration_is_idempotent_and_never_spawns_a_worker(self):
        with patch.object(self.manager, "_start", side_effect=AssertionError("Unexpected worker")):
            first = self.manager.restore_result(self.workspace, self.result, actor=LOCAL_ACTOR, migration_project_name="Customers", expected_job_id=None)
            second = self.manager.restore_result(self.workspace, self.result, actor=LOCAL_ACTOR, migration_project_name="Customers", expected_job_id=first.job_id)
        self.assertEqual(first, second)
        self.assertFalse(self.manager._workers)
        self.assertFalse(self.manager._pending)

    def test_restoration_cannot_replace_a_current_worker(self):
        job = self.job()
        restored = self.manager.restore_result(self.workspace, self.result, actor=LOCAL_ACTOR, migration_project_name="Customers", expected_job_id=job.job_id)
        self.assertEqual(restored.job_id, job.job_id)
        self.assertTrue(restored.active)

    def test_a_result_for_another_mapping_cannot_be_restored(self):
        with self.assertRaisesRegex(ValueError, "another mapping"):
            self.manager.restore_result(self.workspace, replace(self.result, mapping_content_hash=content_hash("older")),
                                        actor=LOCAL_ACTOR, migration_project_name="Customers", expected_job_id=None)
        self.assertEqual(self.manager.latest_many((self.workspace.workspace_id,)), {})

    def test_attempt_completed_during_recovery_read_is_not_replaced(self):
        job = self.job()
        failed = self.manager.registry.mark_failed(job.job_id, "BAD_SOURCE", "Review source")
        restored = self.manager.restore_result(self.workspace, self.result, actor=LOCAL_ACTOR,
                                               migration_project_name="Customers", expected_job_id=None)
        self.assertEqual(restored, failed)

    def test_retry_carries_the_confirmed_recipe_mapping_into_the_worker_request(self):
        previous = self.job()
        self.manager.registry.mark_failed(previous.job_id, "BAD_SOURCE", "Review source")
        context = NS(preparation_jobs=self.manager, actor=LOCAL_ACTOR)
        with patch("impodo.web.run_commands._preparation_workspace", return_value=replace(self.workspace, mapping_content_hash=None)), \
             patch("impodo.web.run_commands._assert_recipe_application_can_prepare", return_value=self.workspace), \
             patch("impodo.web.run_commands.recover_run_preparation", return_value=None), \
             patch("impodo.web.run_commands._preparation_row_count", return_value=10), \
             patch("impodo.web.run_commands._migration_project_name", return_value="Customers"), \
             patch.object(self.manager, "_start"):
            retried = enqueue_preparation(context, self.workspace.workspace_id, retry_job_id=previous.job_id)
        self.assertNotEqual(retried.job_id, previous.job_id)
        self.assertEqual(retried.workspace.mapping_content_hash, self.workspace.mapping_content_hash)

    def test_unknown_retry_does_not_read_or_restore_saved_work(self):
        with patch("impodo.web.run_commands.recover_run_preparation") as recovery:
            with self.assertRaises(PreparationJobNotFoundError):
                enqueue_preparation(NS(preparation_jobs=self.manager), self.workspace.workspace_id, retry_job_id="missing")
        recovery.assert_not_called()


class RunRecoveryBoundaryTests(TestCase):
    def context(self, *, status=RecipeApplicationStatus.RUNNING, job=None):
        self.application = NS(application_id="app", recipe_id="recipe", workspace_id="workspace",
                              mapping_content_hash="mapping", status=status)
        self.later = NS(application_id="later", recipe_id="later-recipe", workspace_id="later-workspace",
                        status=RecipeApplicationStatus.READY)
        bundle = NS(applications=(self.application, self.later),
                    requirement_plan=NS(application_order=("recipe", "later-recipe")))
        return NS(
            actor=LOCAL_ACTOR, load_jobs=None,
            preparation_jobs=Mock(latest_many=Mock(return_value={"workspace": job} if job else {})),
            run_planning=NS(repository=Mock(get_bundle=Mock(return_value=bundle), list_issues=Mock(return_value=()))),
            preparation_recovery=Mock(current=Mock(return_value=None)),
        )

    def test_missing_session_reads_only_the_first_unverified_recipe(self):
        context = self.context()
        recover_run_preparation(context, "run")
        context.preparation_recovery.current.assert_called_once_with("workspace", "mapping", actor=LOCAL_ACTOR)

    def test_active_or_later_progress_does_not_open_workspace_evidence(self):
        cases = ((RecipeApplicationStatus.RUNNING, NS(active=True)),
                 (RecipeApplicationStatus.PREPARED, None), (RecipeApplicationStatus.COMPARED, None),
                 (RecipeApplicationStatus.EXECUTED, None), (RecipeApplicationStatus.BLOCKED, None))
        for status, job in cases:
            with self.subTest(status=status):
                context = self.context(status=status, job=job)
                recover_run_preparation(context, "run")
                context.preparation_recovery.current.assert_not_called()

    def test_retained_failure_is_not_silently_replaced_by_older_success(self):
        context = self.context(job=NS(active=False, status=PreparationJobStatus.FAILED, failure_code="BAD_SOURCE"))
        recover_run_preparation(context, "run")
        context.preparation_recovery.current.assert_not_called()

    def test_load_progress_prevents_preparation_recovery(self):
        context = self.context()
        context.load_jobs = Mock(latest_many=Mock(return_value={"workspace": NS(active=True)}))
        recover_run_preparation(context, "run")
        context.preparation_recovery.current.assert_not_called()

    def test_terminal_snapshot_is_rechecked_after_duplicate_review_changes(self):
        context = self.context(job=NS(active=False, status=PreparationJobStatus.REVIEW_REQUIRED))
        self.assertIsNone(recover_run_preparation(context, "run"))
        context.preparation_recovery.current.assert_called_once()
        context.preparation_jobs.restore_result.assert_not_called()
