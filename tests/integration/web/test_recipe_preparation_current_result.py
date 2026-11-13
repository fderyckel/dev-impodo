"""Resume Recipe reviews only from current evidence or an active attempt."""

from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.web.composition.preparation_job_manager import PreparationJobManager
from impodo.web.run_commands import _preparation_workspace
from tests.integration.web import test_fresh_data_controls as fixtures
from tests.support.identity import resolve_local_actor


class RecipePreparationCurrentResultTests(TestCase):
    def prepared_case(self, major):
        fixture = fixtures.FreshDataControlBrowserTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def configure_recipe(definition):
            contract = definition["odoo_target_contract"]
            contract["odoo_major_version"] = major
            contract["reference_policy_hash"] = reference_policy_hash(major)

        url = fixture.registered_delivery(configure_recipe=configure_recipe)
        page = fixture.client.get(url)
        accepted = fixture.client.post(
            url + "/accept",
            data={
                "csrf_token": fixtures.field(page, "csrf_token"),
                "parameter_revision": "",
                "warnings_acknowledged": "1",
                "control_0": "125.50",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        application = fixture._activate_with_real_compiler(odoo_version=f"{major}.0")
        context = fixture.context
        actor = resolve_local_actor(context)
        application = context.run_planning.repository.transition_application_status(
            application.application_id,
            expected_statuses=(RecipeApplicationStatus.READY,),
            status=RecipeApplicationStatus.PREPARED,
            expected_mapping_content_hash=application.mapping_content_hash,
            actor=actor,
        )
        result = context.preparation_recovery.current(
            application.workspace_id, application.mapping_content_hash, actor=actor
        )
        self.assertIsNotNone(result)
        manager = PreparationJobManager(fixture.fixture.root)
        self.addCleanup(manager.shutdown)
        context.preparation_jobs = manager
        workspace = replace(
            _preparation_workspace(context, application.workspace_id, actor=actor),
            mapping_content_hash=application.mapping_content_hash,
        )
        run_url = (
            f"/projects/{application.project_id}/runs/{application.migration_run_id}"
        )
        return fixture, application, result, manager, workspace, run_url

    def invalidate_saved_result(self, context, application):
        repository = context.preparation_recovery.repository
        path = (
            repository.workspace_directory(application.workspace_id)
            / "workspace-engine.duckdb"
        )
        with repository._connect(path) as connection:
            connection.execute(
                "UPDATE normalization_run SET status = 'INVALIDATED' "
                "WHERE run_id = (SELECT run_id FROM normalization_current "
                "WHERE singleton_id = 1)"
            )

    def test_prepared_restart_with_unavailable_evidence_can_prepare_again(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, application, _, manager, _, run_url = self.prepared_case(major)
                self.invalidate_saved_result(fixture.context, application)
                with patch.object(manager, "_start") as start:
                    resumed = fixture.client.get(
                        f"{run_url}/applications/{application.application_id}",
                        follow_redirects=False,
                    )
                    self.assertEqual(resumed.status_code, 303, resumed.text)
                    self.assertEqual(
                        resumed.headers["location"],
                        f"/workspaces/{application.workspace_id}/prepare",
                    )
                    page = fixture.client.get(resumed.headers["location"])
                    self.assertEqual(page.status_code, 200, page.text)
                    submitted = fixture.client.post(
                        f"/workspaces/{application.workspace_id}/summary/check",
                        data={"csrf_token": fixtures.field(page, "csrf_token")},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
                    self.assertEqual(submitted.status_code, 303, submitted.text)
                    self.assertIn("/preparation/", submitted.headers["location"])
                    latest = manager.latest_many((application.workspace_id,))[
                        application.workspace_id
                    ]
                    self.assertTrue(latest.active)
                    self.assertEqual(
                        latest.workspace.mapping_content_hash,
                        application.mapping_content_hash,
                    )
                    self.assertEqual(
                        fixture.context.run_planning.repository.get_application(
                            application.application_id
                        ).status,
                        RecipeApplicationStatus.RUNNING,
                    )
                    start.assert_called_once()

    def test_unavailable_terminal_snapshots_do_not_drive_resume_or_polling(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, application, result, manager, workspace, run_url = (
                    self.prepared_case(major)
                )
                actor = resolve_local_actor(fixture.context)
                self.invalidate_saved_result(fixture.context, application)
                for status in (
                    PreparationJobStatus.SUCCEEDED,
                    PreparationJobStatus.REVIEW_REQUIRED,
                ):
                    with self.subTest(status=status):
                        job, _ = manager.registry.enqueue(
                            application.workspace_id,
                            "Balances",
                            2,
                            actor.identity,
                            workspace,
                            manager.build_contract,
                        )
                        saved = (
                            manager.registry.mark_succeeded(
                                job.job_id, result.result_run_id
                            )
                            if status is PreparationJobStatus.SUCCEEDED
                            else manager.registry.mark_review_required(
                                job.job_id, result_run_id=result.result_run_id
                            )
                        )
                        resumed = fixture.client.get(
                            f"{run_url}/applications/{application.application_id}",
                            follow_redirects=False,
                        )
                        self.assertTrue(
                            resumed.headers["location"].endswith("/prepare")
                        )
                        self.assertEqual(
                            manager.latest_many((application.workspace_id,)), {}
                        )
                        self.assertEqual(
                            manager.get(application.workspace_id, job.job_id), saved
                        )
                        self.assertEqual(
                            fixture.context.run_planning.repository.get_application(
                                application.application_id
                            ).status,
                            RecipeApplicationStatus.PREPARED,
                        )
                        with patch.object(
                            fixture.context.preparation_recovery,
                            "current_for_review",
                            side_effect=AssertionError(
                                "Polling must not reopen result evidence"
                            ),
                        ):
                            first = fixture.client.get(run_url + "/status")
                            second = fixture.client.get(run_url + "/status")
                        self.assertEqual(first.status_code, 200, first.text)
                        self.assertEqual(
                            first.json()["view_hash"], second.json()["view_hash"]
                        )
                        self.assertIn(
                            "saved Recipe", first.json()["applications"][0]["message"]
                        )

    def test_current_result_after_restart_resumes_without_a_worker(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, application, result, manager, _, run_url = self.prepared_case(
                    major
                )
                with patch.object(
                    manager, "_start", side_effect=AssertionError("Unexpected worker")
                ):
                    resumed = fixture.client.get(
                        f"{run_url}/applications/{application.application_id}",
                        follow_redirects=False,
                    )
                self.assertEqual(resumed.status_code, 303, resumed.text)
                self.assertTrue(resumed.headers["location"].endswith("/normalization"))
                self.assertEqual(
                    manager.latest_many((application.workspace_id,))[
                        application.workspace_id
                    ].result_run_id,
                    result.result_run_id,
                )

    def test_active_attempt_remains_current_without_rechecking_saved_evidence(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, application, _, manager, workspace, run_url = (
                    self.prepared_case(major)
                )
                actor = resolve_local_actor(fixture.context)
                job, _ = manager.registry.enqueue(
                    application.workspace_id,
                    "Balances",
                    2,
                    actor.identity,
                    workspace,
                    manager.build_contract,
                )
                with patch.object(
                    fixture.context.preparation_recovery,
                    "current_for_review",
                    side_effect=AssertionError("An active attempt owns its result"),
                ):
                    resumed = fixture.client.get(
                        f"{run_url}/applications/{application.application_id}",
                        follow_redirects=False,
                    )
                self.assertEqual(resumed.status_code, 303, resumed.text)
                self.assertTrue(
                    resumed.headers["location"].endswith(f"/preparation/{job.job_id}")
                )
                self.assertTrue(
                    manager.get(application.workspace_id, job.job_id).active
                )
