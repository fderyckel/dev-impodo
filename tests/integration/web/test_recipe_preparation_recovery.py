"""Recover actual Recipe publications after lost IPC and registry notification."""

from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.project.foundation import MigrationConflictError
from impodo.domain.serialization import content_hash
from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.web.composition.preparation_job_manager import PreparationJobManager, _run_preparation_worker
from impodo.web.run_commands import _preparation_workspace
from impodo.web.run_commands import publish_preparation_progress
from tests.integration.web import test_fresh_data_controls as fixtures


class _WithoutTerminalEvents:
    def __init__(self, events):
        self.events = events

    def put(self, event):
        if event[0] not in {"succeeded", "review_required", "failed", "cancelled"}:
            self.events.put(event)


def _worker_without_final_notification(root, workspace_id, workspace, build, actor, events, cancel):
    """Fault boundary in a real spawned child; preparation still commits normally."""

    _run_preparation_worker(root, workspace_id, workspace, build, actor, _WithoutTerminalEvents(events), cancel)


class RecipePreparationRecoveryBrowserTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.FreshDataControlBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.context, self.client = self.fixture.context, self.fixture.client
        self.root = self.fixture.fixture.root

    def ready_recipe(self):
        url = self.fixture.registered_delivery()
        page = self.client.get(url)
        accepted = self.client.post(url + "/accept", data={
            "csrf_token": fixtures.field(page, "csrf_token"), "parameter_revision": "",
            "warnings_acknowledged": "1", "control_0": "125.50",
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        return self.fixture._activate_with_real_compiler(prepare=False)

    def test_lost_worker_result_and_registry_notification_resume_without_repreparing(self):
        application = self.ready_recipe()
        context, actor = self.context, self.context.actor
        manager = PreparationJobManager(self.root)
        self.addCleanup(manager.shutdown)
        context.preparation_jobs = manager
        manager.set_result_reader(lambda job: context.preparation_recovery.current(
            job.workspace_id, job.workspace.mapping_content_hash, actor=actor,
        ))

        def lost_registry_notification(job):
            if job.status is PreparationJobStatus.SUCCEEDED:
                raise RuntimeError("Injected loss of the final registry notification")
            publish_preparation_progress(context, job)

        manager.set_status_listener(lost_registry_notification)
        workspace = replace(_preparation_workspace(context, application.workspace_id),
                            mapping_content_hash=application.mapping_content_hash)
        with patch("impodo.web.composition.preparation_job_manager._run_preparation_worker", _worker_without_final_notification):
            job = manager.enqueue(application.workspace_id, "Customer balances", 2, actor=actor, workspace=workspace)
            worker = manager._workers[job.job_id]
            with self.assertLogs("impodo.web.composition.preparation_job_manager", level="ERROR"):
                worker.supervisor.join(timeout=60)
        self.assertFalse(worker.supervisor.is_alive(), "The isolated preparation worker did not finish")
        completed = manager.get(application.workspace_id, job.job_id)
        self.assertEqual(completed.status, PreparationJobStatus.SUCCEEDED, completed.failure_message)
        self.assertEqual(context.run_planning.repository.get_application(application.application_id).status, RecipeApplicationStatus.RUNNING)
        saved = context.normalization.current_summary(application.workspace_id)
        self.assertEqual(completed.result_run_id, saved.run_id)

        # Replace the session registry, as on app restart, while preserving the stores.
        restarted = PreparationJobManager(self.root)
        self.addCleanup(restarted.shutdown)
        context.preparation_jobs = restarted
        route = f"/projects/{application.project_id}/runs/{application.migration_run_id}"
        with patch.object(restarted, "_start", side_effect=AssertionError("Recovery spawned another worker")), \
             patch.object(context.preparation, "prepare", side_effect=AssertionError("Recovery rescanned source rows")), \
             patch.object(context.preparation_recovery, "current", wraps=context.preparation_recovery.current) as read:
            resumed = self.client.get(route + f"/applications/{application.application_id}", follow_redirects=False)
            self.assertEqual(resumed.status_code, 303, resumed.text)
            self.assertTrue(resumed.headers["location"].endswith("/normalization"))
            read.assert_called_once()
            restored = restarted.latest_many((application.workspace_id,))[application.workspace_id]
            self.assertEqual(restored.result_run_id, saved.run_id)
            self.assertEqual(context.run_planning.repository.get_application(application.application_id).status, RecipeApplicationStatus.PREPARED)
            read.reset_mock()
            self.assertEqual(self.client.get(route + "/status").status_code, 200)
            self.assertEqual(self.client.get(route + "/status").status_code, 200)
            read.assert_not_called()
        self.assertEqual(context.normalization.current_summary(application.workspace_id).run_id, saved.run_id)

        # Current pointers alone are insufficient: each dependency and publication must agree.
        recovery = context.preparation_recovery
        with patch.object(recovery.repository, "read_current", wraps=recovery.repository.read_current) as read:
            self.assertIsNotNone(recovery.current(application.workspace_id, application.mapping_content_hash, actor=actor))
        inputs = read.call_args.args[1]
        for field in ("mapping_hash", "physical_selection_hash", "source_selection_hash", "schema_hash", "retention_context_hash"):
            with self.subTest(changed_input=field):
                self.assertIsNone(recovery.repository.read_current(application.workspace_id, replace(inputs, **{field: content_hash("changed")})))
        path = recovery.repository.workspace_directory(application.workspace_id) / "workspace-engine.duckdb"
        for table, column, replacement in (
            ("canonical_staging_run", "status", "PENDING"),
            ("preparation_session", "status", "READY"),
            ("quality_run", "status", "INVALIDATED"),
            ("quality_run", "ruleset_hash", content_hash("other rules")),
            ("normalization_run", "staging_content_hash", content_hash("other staging")),
            ("normalization_run", "quality_content_hash", content_hash("other quality")),
            ("normalization_run", "status", "INVALIDATED"),
        ):
            with self.subTest(incomplete_publication=(table, column)):
                with recovery.repository._connect(path) as connection:
                    original = connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                    connection.execute(f"UPDATE {table} SET {column} = ?", [replacement])
                try:
                    self.assertIsNone(recovery.current(application.workspace_id, application.mapping_content_hash, actor=actor))
                finally:
                    with recovery.repository._connect(path) as connection:
                        connection.execute(f"UPDATE {table} SET {column} = ?", [original])
        with self.assertRaisesRegex(MigrationConflictError, "mapping changed"):
            context.run_planning.repository.transition_application_status(
                application.application_id, expected_statuses=(RecipeApplicationStatus.PREPARED,),
                status=RecipeApplicationStatus.RUNNING, actor=actor,
                expected_mapping_content_hash=content_hash("older mapping"),
            )
        self.assertEqual(context.run_planning.repository.get_application(application.application_id).status, RecipeApplicationStatus.PREPARED)
