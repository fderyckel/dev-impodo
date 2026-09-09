"""Regression evidence for Recipe activation recovery and resume decisions."""

from dataclasses import replace
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.application.run.progress import (
    ApplicationResumeStep, application_is_verified, application_resume_step,
    current_preparation,
)
from impodo.application.run.test_setup_service import TestRunSetupService
from impodo.adapters.duckdb.test_run_repository import TestRunRepository
from impodo.domain.project.foundation import MigrationOperationState
from impodo.domain.run.contracts import RecipeApplicationStatus, RecipeDependency
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.web.run_review import build_integrated_run_review
from impodo.web.run_commands import publish_preparation_progress
from tests.application.run import test_integrated_recipe_runs as fixtures


class RecipeCompositionTests(TestCase):
    def test_web_and_spawned_worker_require_recipe_quality_evidence(self):
        from impodo.adapters.duckdb.recipe_quality_seed_repository import RecipeQualitySeedRepository
        from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
        from impodo.application.workspace.preparation.job_models import PreparationWorkspace
        from impodo.web.app import create_local_app
        from impodo.web.composition.preparation_worker import create_preparation_worker

        root = Path(".tmp") / f"recipe-composition-{uuid4()}"
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        app = create_local_app(
            root, secret_store=MemorySecretStore(), preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False, load_jobs_enabled=False,
        )
        workspace = PreparationWorkspace(
            project_id=str(uuid4()), data_version_id=str(uuid4()), data_version_number=1,
            data_version_purpose="TEST", migration_run_id=str(uuid4()),
            migration_run_purpose="TEST", workspace_id=str(uuid4()), recipe_application_id=str(uuid4()),
        )
        worker = create_preparation_worker(root, workspace=workspace)
        context = app.state.context
        self.assertIs(context.quality.recipe_quality, context.run_planning.compiler.application_state)
        self.assertIs(context.mapping_workspace.recipe_applications, context.quality.recipe_quality)
        self.assertIsInstance(worker.quality.recipe_quality, RecipeQualitySeedRepository)


class RecipeResumeTests(TestCase):
    def test_reconfirming_identical_mapping_preserves_later_milestones(self):
        from impodo.application.run.application_recovery import RunApplicationRecoveryUseCase

        application = NS(
            application_id="app", project_id="project", workspace_id="workspace",
            mapping_id="mapping", mapping_content_hash="hash", status=RecipeApplicationStatus.COMPARED,
        )
        repository, compiler = MagicMock(), MagicMock()
        repository.get_application.return_value = application
        repository.list_issues.return_value = ()
        compiler.mappings.mappings.get_mapping_revision.return_value = NS(
            mapping_id="mapping", version=1, definition=NS(content_hash="hash"),
        )
        compiler.mappings.mappings.get_mapping_submission.return_value = NS(
            mapping_id="mapping", mapping_content_hash="hash",
        )
        service = RunApplicationRecoveryUseCase(
            repository=repository, authorization=MagicMock(), compiler=compiler,
            source_packages=None, data_versions=None, test_run_values=None, recipes=None,
            package_selection=None,
        )
        self.assertIs(service.confirm_mapping("app", actor=LOCAL_ACTOR), application)
        repository.save_application_materialization.assert_not_called()

    def test_stale_preparation_request_stops_before_reading_source_rows(self):
        from impodo.application.workspace.preparation.preparation_service import PreparationService
        from impodo.domain.errors import ReadinessError

        sources, mappings = MagicMock(), MagicMock()
        mappings.get_mapping_revision.return_value = NS(definition=NS(content_hash="new"))
        service = PreparationService(
            MagicMock(), sources, MagicMock(), mappings, MagicMock(), MagicMock(),
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        )
        with self.assertRaisesRegex(ReadinessError, "changed after preparation was requested"):
            service.prepare("workspace", actor=LOCAL_ACTOR, expected_mapping_hash="old")
        sources.get_source_selection.assert_not_called()

    def test_late_worker_notification_cannot_advance_a_changed_mapping(self):
        repository = MagicMock()
        repository.get_application.return_value = NS(mapping_content_hash="new")
        context = NS(run_planning=NS(repository=repository))
        publish_preparation_progress(context, NS(
            workspace=NS(recipe_application_id="app", mapping_content_hash="old"),
        ))
        repository.transition_application_status.assert_not_called()

    def test_restart_keeps_verified_and_compared_work_at_its_own_step(self):
        completed_preparation = NS(status=NS(value="SUCCEEDED"), active=False)
        for status, expected in (
            (RecipeApplicationStatus.RECONCILED, ApplicationResumeStep.LOAD_RESULT),
            (RecipeApplicationStatus.QUALIFIED, ApplicationResumeStep.LOAD_RESULT),
            (RecipeApplicationStatus.EXECUTED, ApplicationResumeStep.LOAD_RESULT),
            (RecipeApplicationStatus.COMPARED, ApplicationResumeStep.LOAD_REVIEW),
        ):
            with self.subTest(status=status):
                application = NS(status=status)
                self.assertEqual(application_resume_step(application), expected)
                self.assertEqual(
                    application_resume_step(application, completed_preparation), expected,
                )

    def test_old_preparation_cannot_block_a_changed_mapping(self):
        application = NS(status=RecipeApplicationStatus.READY, mapping_content_hash="new")
        job = NS(
            workspace=NS(mapping_content_hash="old"),
            status=NS(value="SUCCEEDED"), active=False,
        )
        self.assertIsNone(current_preparation(application, job))
        self.assertEqual(application_resume_step(application, job), ApplicationResumeStep.PREPARE)

    def test_live_load_success_does_not_release_an_unrecorded_dependency(self):
        application = NS(status=RecipeApplicationStatus.EXECUTED)
        self.assertFalse(application_is_verified(application))
        application.status = RecipeApplicationStatus.RECONCILED
        self.assertTrue(application_is_verified(application))

    def test_progress_changes_do_not_require_a_page_reload(self):
        application = NS(
            application_id="app", workspace_id="workspace", recipe_id="recipe",
            project_id="project", migration_run_id="run",
            status=RecipeApplicationStatus.RUNNING,
        )
        job = NS(active=True, message="Preparing rows", progress_percent=10)
        context = NS(preparation_jobs=NS(latest_many=lambda ids: {"workspace": job}), load_jobs=None)
        from impodo.domain.run.models import MigrationRunPurpose
        bundle = NS(
            applications=(application,), run=NS(purpose=MigrationRunPurpose.TEST),
            requirement_plan=NS(application_order=("recipe",)),
        )
        first = build_integrated_run_review(context, bundle, recipes={}, issues={})
        job.progress_percent = 50
        job.message = "Checking rows"
        second = build_integrated_run_review(context, bundle, recipes={}, issues={})
        self.assertEqual(first.view_hash, second.view_hash)
        self.assertNotEqual(first.cards[0].progress_percent, second.cards[0].progress_percent)


class RecipeActivationRecoveryTests(TestCase):
    def test_browser_resume_finishes_plan_and_preserves_published_applications(self):
        fixture = fixtures.IntegratedRecipeRunTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = TestRunSetupService(
            projects=fixture.projects, data_versions=fixture.data_versions, runs=fixture.runs,
            migration_workspaces=fixture.workspaces, source_packages=fixture.packages,
            workspace_states=fixture.workspace_states, recipes=fixture.recipe_service,
            test_runs=TestRunRepository(fixture.foundation), run_planning=fixture.planning,
            authorization=fixture.authorization,
        )
        project_id = fixture.bundle.project.project_id
        setup = service.start_setup(
            project_id,
            expected_workspace_revision=fixture.projects.get(project_id, actor=LOCAL_ACTOR).optimistic_revision,
            recipe_revisions=fixture._selected(), dependencies=(RecipeDependency(
                before_recipe_id=fixture.customer.recipe.recipe_id,
                after_recipe_id=fixture.product.recipe.recipe_id,
            ),), label="Recovery regression", export_as_of="2026-08-24",
            operation_id=str(uuid4()), actor=LOCAL_ACTOR,
        )
        service.replace_fresh_data_run_values(
            setup.binding, {"parameter:batch_reference": "RECOVERY"},
            expected_revision=None, actor=LOCAL_ACTOR,
        )
        fixture._replace_and_freeze(setup.data_version, expected_package_revision=1)
        state = fixture.workspace_states.repository.get(setup.setup_workspace.workspace_id)
        fixture.workspace_states.update_target(
            state.workspace_id, actor=LOCAL_ACTOR, expected_revision=state.revision,
            odoo_connection_mode="REMOTE", odoo_base_url="https://preprod.example.test",
            odoo_database="preprod", intended_applications=(),
            intended_models=("product.template", "res.partner"),
        )
        operation_id = str(uuid4())

        def crash(stage):
            if stage == "REGISTRY_COMMITTED":
                raise fixtures.SimulatedCrash(stage)

        with self.assertRaises(fixtures.SimulatedCrash):
            fixture.planning.activate_test_run(
                project_id,
                expected_workspace_revision=fixture.projects.get(project_id, actor=LOCAL_ACTOR).optimistic_revision,
                test_binding=setup.binding,
                target_schema=replace(fixture.schema, workspace_id=state.workspace_id),
                target_reference_bundle=None,
                credential_generation=fixture.schema.read_credential_binding_hash,
                parameter_values=service._fresh_data.activation_values(
                    setup.binding, "2026-08-24", actor=LOCAL_ACTOR,
                ).parameters, operation_id=operation_id, actor=LOCAL_ACTOR, fault=crash,
            )
        with patch.object(fixture.cutover_repository, "ensure_for_run", side_effect=fixtures.SimulatedCrash("plan")):
            with self.assertRaises(fixtures.SimulatedCrash):
                service.resume_activation_if_needed(setup.run.migration_run_id, actor=LOCAL_ACTOR)
        self.assertEqual(fixture.foundation.get_operation_intent(operation_id).state, MigrationOperationState.PENDING)
        before = fixture.planning_repository.get_bundle(setup.run.migration_run_id)
        self.assertTrue(all(item.mapping_id for item in before.applications))
        with patch.object(fixture.compiler, "materialize", side_effect=AssertionError("recompiled published work")):
            recovered = service.resume_activation_if_needed(setup.run.migration_run_id, actor=LOCAL_ACTOR)
        self.assertEqual(recovered.applications, before.applications)
        self.assertIsNotNone(fixture.cutover_repository.get_run_binding(recovered.run.migration_run_id))
        self.assertEqual(fixture.foundation.get_operation_intent(operation_id).state, MigrationOperationState.COMMITTED)
        self.assertIsNone(service.resume_activation_if_needed(setup.run.migration_run_id, actor=LOCAL_ACTOR))
