from __future__ import annotations

from dataclasses import replace

from tests.support.paths import REPOSITORY_ROOT

from pathlib import Path
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import duckdb

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.application.shared.build_contract import (
    ApplicationBuildContract,
    PROCESS_BUILD_CONTRACT,
)
from impodo.application.workspace.preparation.preparation_job_registry import (
    PreparationJobNotFoundError,
    PreparationJobRegistry,
    PreparationJobStateError,
)
from impodo.web.composition.preparation_job_manager import (
    PreparationCancelled,
    PreparationJobManager,
    _run_preparation_worker,
)
from impodo.application.workspace.preparation.preparation_service import (
    PreparationService,
)
from impodo.adapters.polars_transformation import PolarsTransformationAdapter
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.run.models import MigrationRunPurpose
from impodo.application.workspace.preparation.job_models import (
    PreparationJobStatus,
    PreparationPhase,
    PreparationWorkspace,
)
from impodo.web.routers.preparation import (
    _assert_recipe_application_can_prepare,
    _preparation_workspace,
)
from impodo.domain.workspace.errors import WorkspaceError


ROOT = REPOSITORY_ROOT
TEST_BUILD_CONTRACT = ApplicationBuildContract(
    application_build_id="sha256:" + "1" * 64,
    workspace_schema_generation="test-workspace-generation",
    workspace_schema_version=1,
)


def _workspace() -> PreparationWorkspace:
    return PreparationWorkspace(
        project_id=str(uuid4()),
        data_version_id=str(uuid4()),
        data_version_number=1,
        data_version_purpose=DataVersionPurpose.AUTHORING,
        migration_run_id=str(uuid4()),
        migration_run_purpose=MigrationRunPurpose.AUTHORING,
        workspace_id=str(uuid4()),
    )


class PreparationJobRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = PreparationJobRegistry()
        self.workspace_id = str(uuid4())

    def test_one_active_attempt_progress_cancel_and_retry(self) -> None:
        queued, created = self.registry.enqueue(
            self.workspace_id,
            "Large products",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
            TEST_BUILD_CONTRACT,
        )
        repeated, repeated_created = self.registry.enqueue(
            self.workspace_id,
            "Large products",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
            TEST_BUILD_CONTRACT,
        )

        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated.job_id, queued.job_id)
        running = self.registry.mark_running(queued.job_id)
        self.assertEqual(running.status, PreparationJobStatus.RUNNING)
        progressed = self.registry.update_progress(
            queued.job_id,
            PreparationPhase.TRANSFORMING,
            completed_rows=5_000,
            total_rows=100_000,
        )
        self.assertEqual(progressed.completed_rows, 5_000)
        self.assertGreater(progressed.progress_percent, 5)

        stopping = self.registry.request_cancel(self.workspace_id, queued.job_id)
        self.assertTrue(stopping.cancel_requested)
        stopped = self.registry.mark_cancelled(queued.job_id)
        self.assertEqual(stopped.status, PreparationJobStatus.CANCELLED)

        retry, retry_created = self.registry.enqueue(
            self.workspace_id,
            "Large products",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
            TEST_BUILD_CONTRACT,
        )
        self.assertTrue(retry_created)
        self.assertEqual(retry.attempt, 2)
        self.assertNotEqual(retry.job_id, queued.job_id)
        self.assertEqual(
            self.registry.latest_many((self.workspace_id,))[self.workspace_id],
            retry,
        )

    def test_terminal_job_cannot_be_reopened_and_state_is_session_scoped(self) -> None:
        queued, _created = self.registry.enqueue(
            self.workspace_id,
            "Large BOM",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
            TEST_BUILD_CONTRACT,
        )
        self.registry.mark_running(queued.job_id)
        self.registry.mark_failed(
            queued.job_id,
            "WORKER_EXITED",
            "Preparation stopped unexpectedly",
        )
        with self.assertRaises(PreparationJobStateError):
            self.registry.mark_running(queued.job_id)

        fresh_session = PreparationJobRegistry()
        with self.assertRaises(PreparationJobNotFoundError):
            fresh_session.get(self.workspace_id, queued.job_id)

    def test_build_and_contract_failures_cannot_be_retried_blindly(self) -> None:
        for failure_code in (
            "IMPODO_BUILD_CHANGED",
            "WorkspaceStateCompatibilityError",
        ):
            queued, _created = self.registry.enqueue(
                self.workspace_id,
                "Customers",
                999,
                LOCAL_ACTOR.identity,
                _workspace(),
                TEST_BUILD_CONTRACT,
            )
            failed = self.registry.mark_failed(
                queued.job_id,
                failure_code,
                "Restart Impodo before continuing",
            )
            self.assertFalse(failed.retry_allowed)

    def test_recipe_review_blocks_preparation_before_worker_enqueue(self) -> None:
        application_id = str(uuid4())
        workspace_id = str(uuid4())
        application = SimpleNamespace(
            status=RecipeApplicationStatus.BLOCKED,
            workspace_id=workspace_id,
            application_id=application_id,
            recipe_id="recipe",
            mapping_content_hash="sha256:" + "1" * 64,
        )
        default_review = SimpleNamespace(
            code="RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE",
            level=SimpleNamespace(value="REVIEW"),
        )
        repository = SimpleNamespace(
            get_application=lambda current_id: application,
            list_issues=lambda current_id: (default_review,),
            get_bundle=lambda run_id: SimpleNamespace(
                applications=(application,),
                requirement_plan=SimpleNamespace(application_order=("recipe",)),
            ),
        )
        context = SimpleNamespace(
            run_planning=SimpleNamespace(repository=repository)
        )
        workspace = replace(
            _workspace(),
            recipe_application_id=application_id,
            workspace_id=workspace_id,
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "Review the current Odoo defaults",
        ):
            _assert_recipe_application_can_prepare(context, workspace)

        application.status = RecipeApplicationStatus.READY
        repository.list_issues = lambda current_id: (
            SimpleNamespace(
                code="RECIPE_SOURCE_COLUMN_UNUSED",
                level=SimpleNamespace(value="INFORMATION"),
            ),
        )
        accepted = _assert_recipe_application_can_prepare(context, workspace)
        self.assertEqual(accepted.mapping_content_hash, application.mapping_content_hash)

        earlier = SimpleNamespace(
            application_id="earlier-application",
            recipe_id="earlier-recipe",
            status=RecipeApplicationStatus.EXECUTED,
        )
        repository.get_bundle = lambda run_id: SimpleNamespace(
            applications=(application, earlier),
            requirement_plan=SimpleNamespace(application_order=("earlier-recipe", "recipe")),
        )
        with self.assertRaisesRegex(WorkspaceError, "Finish and verify the earlier Recipe"):
            _assert_recipe_application_can_prepare(context, workspace)
        earlier.status = RecipeApplicationStatus.RECONCILED
        accepted = _assert_recipe_application_can_prepare(context, workspace)
        self.assertEqual(accepted.mapping_content_hash, application.mapping_content_hash)


class PreparationWorkspaceProjectionTests(unittest.TestCase):
    def test_worker_packet_includes_the_exact_workspace_source_projection(self) -> None:
        base = _workspace()
        projection = SimpleNamespace(
            package_hash="sha256:" + "2" * 64,
            datasets=(
                SimpleNamespace(dataset_id="customers"),
                SimpleNamespace(dataset_id="addresses"),
            ),
        )
        context = SimpleNamespace(
            actor=LOCAL_ACTOR,
            migration_workspaces=SimpleNamespace(
                get=lambda workspace_id, **kwargs: SimpleNamespace(
                    data_version_id=base.data_version_id,
                    migration_run_id=base.migration_run_id,
                )
            ),
            data_versions=SimpleNamespace(
                get=lambda data_version_id, **kwargs: object()
            ),
            migration_runs=SimpleNamespace(
                get=lambda migration_run_id, **kwargs: object()
            ),
            data_version_source_projection=SimpleNamespace(
                projections=SimpleNamespace(
                    repository=SimpleNamespace(
                        get_workspace_source_projection=lambda workspace_id: projection
                    )
                )
            ),
        )
        with patch.object(
            PreparationWorkspace,
            "from_context",
            return_value=base,
        ):
            result = _preparation_workspace(context, base.workspace_id)

        self.assertEqual(result.source_package_hash, projection.package_hash)
        self.assertEqual(
            result.source_dataset_ids,
            ("addresses", "customers"),
        )


class PreparationCancellationBoundaryTests(unittest.TestCase):
    def test_batch_cancellation_stops_before_canonical_publication(self) -> None:
        selection = SimpleNamespace(
            datasets=(
                SimpleNamespace(
                    source=FileSourceBinding(
                        file_id="source-file",
                        table_key="csv",
                        source_sha256="sha256:" + "1" * 64,
                        catalog_hash="sha256:" + "2" * 64,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=100_000,
                ),
            ),
        )
        definition = SimpleNamespace(content_hash="sha256:" + "2" * 64)
        workspaces = MagicMock()
        workspaces.get.return_value = SimpleNamespace(workspace_id="workspace-id")
        sources = MagicMock()
        sources.get_source_selection.return_value = selection
        sources.get_mapping_source_selection.return_value = selection
        mappings = MagicMock()
        mappings.get_mapping_revision.return_value = SimpleNamespace(
            version=1,
            definition=definition,
        )
        mappings.get_mapping_submission.return_value = SimpleNamespace(
            mapping_content_hash=definition.content_hash,
        )
        derived = MagicMock()
        derived.get_derived_entity_plan.return_value = None
        staging = MagicMock()
        service = PreparationService(
            workspaces,
            sources,
            derived,
            mappings,
            staging,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            PolarsTransformationAdapter(),
            resolution=None,
        )
        checkpoints = 0

        def cancel_after_first_batch() -> None:
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 3:
                raise PreparationCancelled("stop")

        def bounded_batch(*_args, **kwargs):
            kwargs["batch_progress"](5_000, 100_000)
            raise AssertionError("cancellation should interrupt the batch callback")

        with (
            patch(
                "impodo.application.workspace.preparation.preparation_service."
                "supports_bounded_direct_preparation",
                return_value=True,
            ),
            patch(
                "impodo.application.workspace.preparation.preparation_service."
                "compile_preparation_capability",
                return_value=SimpleNamespace(
                    require_supported=lambda: None,
                    permits_materialized_fallback=False,
                ),
            ),
            patch(
                "impodo.application.workspace.preparation.preparation_service."
                "prepare_bounded_direct_session",
                side_effect=bounded_batch,
            ),
            self.assertRaises(PreparationCancelled),
        ):
            service.prepare(
                "workspace-id",
                actor=LOCAL_ACTOR,
                cancellation_checkpoint=cancel_after_first_batch,
            )

        staging.publish_canonical_staging.assert_not_called()


class _RecordingPreparationJobManager(PreparationJobManager):
    def __init__(self, root: str) -> None:
        self.started: list[str] = []
        super().__init__(root, max_workers=1)

    def _start(self, job, actor) -> None:
        del actor
        self.started.append(job.job_id)
        self._workers[job.job_id] = SimpleNamespace(
            process=SimpleNamespace(is_alive=lambda: True)
        )


class PreparationJobSchedulingTests(unittest.TestCase):
    def test_manager_does_not_create_a_second_database(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        temporary = ROOT / ".tmp" / f"preparation-manager-{uuid4()}"
        temporary.mkdir()
        try:
            PreparationJobManager(str(temporary))
            self.assertEqual(tuple(temporary.iterdir()), ())
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def test_local_manager_starts_only_one_memory_heavy_worker(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        temporary = ROOT / ".tmp" / f"preparation-scheduling-{uuid4()}"
        temporary.mkdir()
        try:
            manager = _RecordingPreparationJobManager(str(temporary))
            first = manager.enqueue(
                str(uuid4()),
                "Products",
                100_000,
                actor=LOCAL_ACTOR,
                workspace=_workspace(),
            )
            second = manager.enqueue(
                str(uuid4()),
                "BOM",
                100_000,
                actor=LOCAL_ACTOR,
                workspace=_workspace(),
            )

            self.assertEqual(manager.started, [first.job_id])
            self.assertEqual(
                manager.get(second.workspace_id, second.job_id).status,
                PreparationJobStatus.QUEUED,
            )
            with manager._lock:
                manager._workers.pop(first.job_id)
                manager._schedule_locked()
            self.assertEqual(manager.started, [first.job_id, second.job_id])
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def test_manager_records_value_free_worker_timing(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        temporary = ROOT / ".tmp" / f"preparation-timing-{uuid4()}"
        temporary.mkdir()
        try:
            recorder = MagicMock()
            manager = PreparationJobManager(
                str(temporary),
                diagnostic_recorder=recorder,
            )

            terminal = manager._handle_event(
                "unused-job-id",
                (
                    "timing",
                    "normalization_aggregation",
                    125.5,
                    "completed",
                    "",
                    500,
                    500,
                ),
            )

            self.assertFalse(terminal)
            recorder.record_operation_stage.assert_called_once_with(
                "preparation",
                "normalization_aggregation",
                duration_ms=125.5,
                outcome="completed",
                reason=None,
                completed_rows=500,
                total_rows=500,
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


class PreparationWorkerFailureTests(unittest.TestCase):
    def test_worker_emits_phase_subphase_and_total_timings(self) -> None:
        events = MagicMock()
        cancel = MagicMock()
        cancel.is_set.return_value = False
        preparation = MagicMock()

        def prepare(_workspace_id, *, progress, timing, **_kwargs):
            progress(PreparationPhase.VALIDATING, 0, 12, "Checking")
            progress(PreparationPhase.TRANSFORMING, 12, 12, "Preparing")
            timing("quality_evaluation", 4.5, "completed")
            progress(PreparationPhase.COMPLETE, 12, 12, "Ready")
            return SimpleNamespace(run_id="normalization-run")

        preparation.prepare.side_effect = prepare
        with patch(
            "impodo.web.composition.preparation_worker.create_preparation_worker",
            return_value=preparation,
        ):
            _run_preparation_worker(
                "impodo-root",
                "workspace-id",
                _workspace(),
                PROCESS_BUILD_CONTRACT,
                LOCAL_ACTOR,
                events,
                cancel,
            )

        emitted = [call.args[0] for call in events.put.call_args_list]
        timings = [event for event in emitted if event[0] == "timing"]
        self.assertEqual(
            [event[1] for event in timings],
            [
                PreparationPhase.VALIDATING.value,
                "quality_evaluation",
                PreparationPhase.TRANSFORMING.value,
                "total",
            ],
        )
        self.assertTrue(all(event[2] >= 0 for event in timings))
        self.assertEqual(timings[-1][3], "succeeded")
        self.assertEqual(timings[-1][5:], (12, 12))
        self.assertEqual(emitted[-1], ("succeeded", "normalization-run"))

    def test_local_storage_io_failure_has_safe_actionable_message(self) -> None:
        events = MagicMock()
        cancel = MagicMock()

        with patch(
            "impodo.web.composition.preparation_worker.create_preparation_worker",
            side_effect=duckdb.IOException("IO Error: No space left on device"),
        ):
            _run_preparation_worker(
                "impodo-root",
                "workspace-id",
                _workspace(),
                PROCESS_BUILD_CONTRACT,
                LOCAL_ACTOR,
                events,
                cancel,
            )

        self.assertEqual(events.put.call_args_list[0].args[0], ("started",))
        failure = next(
            call.args[0]
            for call in events.put.call_args_list
            if call.args[0][0] == "failed"
        )
        self.assertEqual(failure[0], "failed")
        self.assertEqual(failure[1], "LOCAL_WORKSPACE_STORAGE_IO_FAILED")
        self.assertIn("No Odoo records were changed", failure[2])
        self.assertIn("free space", failure[2])

    def test_changed_build_stops_before_worker_composition(self) -> None:
        events = MagicMock()
        cancel = MagicMock()
        changed = ApplicationBuildContract(
            application_build_id="sha256:" + "0" * 64,
            workspace_schema_generation=(
                PROCESS_BUILD_CONTRACT.workspace_schema_generation
            ),
            workspace_schema_version=(
                PROCESS_BUILD_CONTRACT.workspace_schema_version
            ),
        )

        with patch(
            "impodo.web.composition.preparation_worker.create_preparation_worker"
        ) as create_worker:
            _run_preparation_worker(
                "impodo-root",
                "workspace-id",
                _workspace(),
                changed,
                LOCAL_ACTOR,
                events,
                cancel,
            )

        create_worker.assert_not_called()
        self.assertEqual(events.put.call_args_list[0].args[0], ("started",))
        failure = next(
            call.args[0]
            for call in events.put.call_args_list
            if call.args[0][0] == "failed"
        )
        self.assertEqual(failure[0], "failed")
        self.assertEqual(failure[1], "IMPODO_BUILD_CHANGED")
        self.assertIn("Restart Impodo", failure[2])


if __name__ == "__main__":
    unittest.main()
