"""Exercise worker exit, restoration, and bounded run recovery decisions."""

from dataclasses import replace
from queue import Empty
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.preparation.job_models import (
    PreparationJobStatus,
    PreparationWorkspace,
)
from impodo.application.workspace.preparation.preparation_job_registry import (
    PreparationJobNotFoundError,
)
from impodo.application.workspace.preparation.recovery import RecoveredPreparation
from impodo.application.workspace.source_readiness import SourceStageIssue
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR, AuthorizationError, Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.preparation_job_manager import PreparationJobManager
from impodo.web.run_commands import enqueue_preparation, recover_run_preparation


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
        self.manager._supervise(job.job_id, process, events, LOCAL_ACTOR)
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
        context = NS(
            preparation_jobs=self.manager,
            actor=LOCAL_ACTOR,
            mapping_workspace=NS(authorization=Mock()),
            queries=NS(get_odoo_schema_catalog=Mock(return_value=None)),
            navigation=NS(
                get_source_readiness=Mock(return_value=NS(ready=True))
            ),
            run_planning=NS(repository=Mock(
                get_application=Mock(return_value=NS(status=RecipeApplicationStatus.READY)),
            )),
        )
        with patch("impodo.web.run_commands._preparation_workspace", return_value=replace(self.workspace, mapping_content_hash=None)), \
             patch("impodo.web.run_commands._assert_recipe_application_can_prepare", return_value=self.workspace), \
             patch("impodo.web.run_commands.recover_run_preparation", return_value=None), \
             patch("impodo.web.run_commands._preparation_row_count", return_value=10), \
             patch("impodo.web.run_commands._migration_project_name", return_value="Customers"), \
             patch.object(self.manager, "_start"):
            retried = enqueue_preparation(
                context,
                self.workspace.workspace_id,
                actor=LOCAL_ACTOR,
                retry_job_id=previous.job_id,
            )
        self.assertNotEqual(retried.job_id, previous.job_id)
        self.assertEqual(retried.workspace.mapping_content_hash, self.workspace.mapping_content_hash)

    def test_incomplete_source_is_rejected_before_preparation_is_resolved(self):
        context = NS(
            preparation_jobs=self.manager,
            mapping_workspace=NS(authorization=Mock()),
            navigation=NS(
                get_source_readiness=Mock(
                    return_value=NS(
                        ready=False,
                        issue=SourceStageIssue.CAPTURE_PLANS_INCOMPLETE,
                    )
                )
            ),
        )
        with (
            patch("impodo.web.run_commands._preparation_workspace") as workspace,
            self.assertRaisesRegex(
                WorkspaceError,
                "Complete every Odoo capture plan",
            ),
        ):
            enqueue_preparation(
                context, self.workspace.workspace_id, actor=LOCAL_ACTOR,
            )
        workspace.assert_not_called()

    def test_command_permission_is_checked_before_admission_or_job_dispatch(self):
        denial = AuthorizationError("Preparation is not permitted")
        authorization = Mock(require=Mock(side_effect=denial))
        readiness = Mock()
        context = NS(
            preparation_jobs=self.manager,
            mapping_workspace=NS(authorization=authorization),
            navigation=NS(get_source_readiness=readiness),
        )
        with self.assertRaises(AuthorizationError) as raised:
            enqueue_preparation(context, self.workspace.workspace_id, actor=LOCAL_ACTOR)
        self.assertIs(raised.exception, denial)
        authorization.require.assert_called_once_with(
            LOCAL_ACTOR, Capability.MAPPING_SUBMIT,
            workspace_id=self.workspace.workspace_id,
        )
        readiness.assert_not_called()
        self.assertEqual(self.manager.latest_many((self.workspace.workspace_id,)), {})

    def test_unknown_retry_does_not_read_or_restore_saved_work(self):
        with (
            patch("impodo.web.run_commands.recover_run_preparation") as recovery,
            self.assertRaises(PreparationJobNotFoundError),
        ):
            enqueue_preparation(
                NS(preparation_jobs=self.manager),
                self.workspace.workspace_id,
                actor=LOCAL_ACTOR,
                retry_job_id="missing",
            )
        recovery.assert_not_called()

    def test_unavailable_result_retains_history_without_reviving_an_older_result(self):
        first = self.job()
        self.manager.registry.mark_succeeded(first.job_id, self.result.result_run_id)
        second = self.job()
        saved = self.manager.registry.mark_succeeded(second.job_id, "later-result")
        self.assertIsNone(self.manager.invalidate_result(
            self.workspace.workspace_id, expected_job_id=second.job_id,
        ))
        self.assertEqual(self.manager.latest_many((self.workspace.workspace_id,)), {})
        self.assertEqual(self.manager.get(self.workspace.workspace_id, second.job_id), saved)
        self.assertEqual(
            self.manager.get(self.workspace.workspace_id, first.job_id).result_run_id,
            self.result.result_run_id,
        )

    def test_unavailable_result_read_cannot_hide_an_active_or_newer_attempt(self):
        first = self.job()
        self.assertEqual(self.manager.invalidate_result(
            self.workspace.workspace_id, expected_job_id=first.job_id,
        ).job_id, first.job_id)
        self.manager.registry.mark_succeeded(first.job_id, self.result.result_run_id)
        second = self.job()
        self.assertEqual(self.manager.invalidate_result(
            self.workspace.workspace_id, expected_job_id=first.job_id,
        ).job_id, second.job_id)
        self.assertTrue(self.manager.latest_many((self.workspace.workspace_id,))[
            self.workspace.workspace_id
        ].active)

    def test_revalidated_result_becomes_visible_and_prunes_the_unavailable_marker(self):
        first = self.job()
        self.manager.registry.mark_succeeded(first.job_id, self.result.result_run_id)
        self.manager.invalidate_result(
            self.workspace.workspace_id, expected_job_id=first.job_id,
        )
        restored = self.manager.restore_result(
            self.workspace, self.result, actor=LOCAL_ACTOR,
            migration_project_name="Customers", expected_job_id=None,
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.result_run_id, self.result.result_run_id)
        self.assertEqual(self.manager.latest_many((self.workspace.workspace_id,))[
            self.workspace.workspace_id
        ], restored)
        self.assertEqual(self.manager.registry._unavailable_results, {})
        self.assertEqual(
            self.manager.get(self.workspace.workspace_id, first.job_id).result_run_id,
            self.result.result_run_id,
        )

    def test_workspace_history_cleanup_prunes_unavailable_result_markers(self):
        first = self.job()
        self.manager.registry.mark_review_required(
            first.job_id, result_run_id=self.result.result_run_id,
        )
        self.manager.invalidate_result(
            self.workspace.workspace_id, expected_job_id=first.job_id,
        )
        self.manager.registry.delete_workspace_history(self.workspace.workspace_id)
        self.assertEqual(self.manager.registry._unavailable_results, {})
        self.assertEqual(self.manager.latest_many((self.workspace.workspace_id,)), {})

    def test_read_only_resume_restores_presentation_without_writing_a_milestone(self):
        actor = replace(LOCAL_ACTOR, capabilities=frozenset({Capability.PROJECT_VIEW}))
        application = NS(
            application_id=self.workspace.recipe_application_id,
            project_id=self.workspace.project_id,
            recipe_id="recipe",
            workspace_id=self.workspace.workspace_id,
            mapping_content_hash=self.workspace.mapping_content_hash,
            status=RecipeApplicationStatus.PREPARED,
        )
        bundle = NS(
            applications=(application,),
            requirement_plan=NS(application_order=("recipe",)),
        )
        repository = Mock(get_bundle=Mock(return_value=bundle), list_issues=Mock(return_value=()))
        context = NS(
            preparation_jobs=self.manager,
            load_jobs=None,
            run_planning=NS(repository=repository),
            preparation_recovery=NS(current_for_review=Mock(return_value=self.result)),
        )
        with (
            patch("impodo.web.run_commands._preparation_workspace", return_value=self.workspace),
            patch("impodo.web.run_commands._migration_project_name", return_value="Customers"),
            patch("impodo.web.run_commands.publish_preparation_progress") as publish,
        ):
            restored = recover_run_preparation(context, self.workspace.migration_run_id, actor=actor)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.result_run_id, self.result.result_run_id)
        publish.assert_not_called()
        repository.transition_application_status.assert_not_called()


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
            preparation_jobs=Mock(
                latest_many=Mock(return_value={"workspace": job} if job else {}),
                invalidate_result=Mock(return_value=None),
            ),
            run_planning=NS(repository=Mock(get_bundle=Mock(return_value=bundle), list_issues=Mock(return_value=()))),
            preparation_recovery=Mock(
                current=Mock(return_value=None),
                current_for_review=Mock(return_value=None),
            ),
        )

    def test_missing_session_reads_only_the_first_unverified_recipe(self):
        context = self.context()
        recover_run_preparation(context, "run", actor=LOCAL_ACTOR)
        context.preparation_recovery.current_for_review.assert_called_once_with("workspace", "mapping", actor=LOCAL_ACTOR)

    def test_active_or_later_progress_does_not_open_workspace_evidence(self):
        cases = ((RecipeApplicationStatus.RUNNING, NS(active=True)),
                 (RecipeApplicationStatus.COMPARED, None),
                 (RecipeApplicationStatus.EXECUTED, None), (RecipeApplicationStatus.BLOCKED, None))
        for status, job in cases:
            with self.subTest(status=status):
                context = self.context(status=status, job=job)
                recover_run_preparation(context, "run", actor=LOCAL_ACTOR)
                context.preparation_recovery.current_for_review.assert_not_called()

    def test_prepared_milestone_requires_current_published_evidence(self):
        context = self.context(status=RecipeApplicationStatus.PREPARED)
        self.assertIsNone(recover_run_preparation(context, "run", actor=LOCAL_ACTOR))
        context.preparation_recovery.current_for_review.assert_called_once_with(
            "workspace", "mapping", actor=LOCAL_ACTOR,
        )
        context.preparation_jobs.invalidate_result.assert_called_once_with(
            "workspace", expected_job_id=None,
        )

    def test_retained_failure_is_not_silently_replaced_by_older_success(self):
        context = self.context(job=NS(active=False, status=PreparationJobStatus.FAILED, failure_code="BAD_SOURCE"))
        recover_run_preparation(context, "run", actor=LOCAL_ACTOR)
        context.preparation_recovery.current_for_review.assert_not_called()

    def test_load_progress_prevents_preparation_recovery(self):
        context = self.context()
        context.load_jobs = Mock(latest_many=Mock(return_value={"workspace": NS(active=True)}))
        recover_run_preparation(context, "run", actor=LOCAL_ACTOR)
        context.preparation_recovery.current_for_review.assert_not_called()

    def test_terminal_snapshot_is_rechecked_after_duplicate_review_changes(self):
        context = self.context(job=NS(
            job_id="completed-job", active=False,
            status=PreparationJobStatus.REVIEW_REQUIRED,
        ))
        self.assertIsNone(
            recover_run_preparation(context, "run", actor=LOCAL_ACTOR)
        )
        context.preparation_recovery.current_for_review.assert_called_once()
        context.preparation_jobs.restore_result.assert_not_called()
        context.preparation_jobs.invalidate_result.assert_called_once_with(
            "workspace", expected_job_id="completed-job",
        )

    def test_preparation_commands_keep_submission_authorization(self):
        context = self.context()
        recover_run_preparation(context, "run", actor=LOCAL_ACTOR, for_review=False)
        context.preparation_recovery.current.assert_called_once_with(
            "workspace", "mapping", actor=LOCAL_ACTOR,
        )
        context.preparation_recovery.current_for_review.assert_not_called()
        context.preparation_jobs.restore_result.assert_not_called()
