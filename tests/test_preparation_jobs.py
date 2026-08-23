from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import duckdb

from impodo.access import LOCAL_ACTOR
from impodo.application.preparation_job_registry import (
    PreparationJobNotFoundError,
    PreparationJobRegistry,
    PreparationJobStateError,
)
from impodo.application.preparation_job_service import PreparationCancelled
from impodo.application.preparation_job_service import PreparationJobManager
from impodo.application.preparation_job_service import _run_preparation_worker
from impodo.application.preparation_service import PreparationService
from impodo.domain.source_binding import FileSourceBinding
from impodo.data_versions import DataVersionPurpose
from impodo.preparation_jobs import (
    PreparationJobStatus,
    PreparationPhase,
    PreparationWorkspace,
)


ROOT = Path(__file__).resolve().parents[1]


def _workspace() -> PreparationWorkspace:
    return PreparationWorkspace(
        project_id=str(uuid4()),
        data_version_id=str(uuid4()),
        data_version_number=1,
        data_version_purpose=DataVersionPurpose.AUTHORING,
        migration_run_id=str(uuid4()),
        workspace_id=str(uuid4()),
    )


class PreparationJobRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = PreparationJobRegistry()
        self.project_id = str(uuid4())

    def test_one_active_attempt_progress_cancel_and_retry(self) -> None:
        queued, created = self.registry.enqueue(
            self.project_id,
            "Large products",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
        )
        repeated, repeated_created = self.registry.enqueue(
            self.project_id,
            "Large products",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
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

        stopping = self.registry.request_cancel(self.project_id, queued.job_id)
        self.assertTrue(stopping.cancel_requested)
        stopped = self.registry.mark_cancelled(queued.job_id)
        self.assertEqual(stopped.status, PreparationJobStatus.CANCELLED)

        retry, retry_created = self.registry.enqueue(
            self.project_id,
            "Large products",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
        )
        self.assertTrue(retry_created)
        self.assertEqual(retry.attempt, 2)
        self.assertNotEqual(retry.job_id, queued.job_id)

    def test_terminal_job_cannot_be_reopened_and_state_is_session_scoped(self) -> None:
        queued, _created = self.registry.enqueue(
            self.project_id,
            "Large BOM",
            100_000,
            LOCAL_ACTOR.identity,
            _workspace(),
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
            fresh_session.get(self.project_id, queued.job_id)


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
        projects = MagicMock()
        projects.get.return_value = SimpleNamespace(project_id="project-id")
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
            projects,
            sources,
            derived,
            mappings,
            staging,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
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
                "impodo.application.preparation_service.supports_bounded_direct_preparation",
                return_value=True,
            ),
            patch(
                "impodo.application.preparation_service."
                "compile_preparation_capability",
                return_value=SimpleNamespace(
                    require_supported=lambda: None,
                    permits_materialized_fallback=False,
                ),
            ),
            patch(
                "impodo.application.preparation_service.prepare_bounded_direct_session",
                side_effect=bounded_batch,
            ),
            self.assertRaises(PreparationCancelled),
        ):
            service.prepare(
                "project-id",
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
                manager.get(second.project_id, second.job_id).status,
                PreparationJobStatus.QUEUED,
            )
            with manager._lock:
                manager._workers.pop(first.job_id)
                manager._schedule_locked()
            self.assertEqual(manager.started, [first.job_id, second.job_id])
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


class PreparationWorkerFailureTests(unittest.TestCase):
    def test_local_storage_io_failure_has_safe_actionable_message(self) -> None:
        events = MagicMock()
        cancel = MagicMock()

        with patch(
            "impodo.preparation_worker.create_preparation_worker",
            side_effect=duckdb.IOException("IO Error: No space left on device"),
        ):
            _run_preparation_worker(
                "project-root",
                "project-id",
                _workspace(),
                LOCAL_ACTOR,
                events,
                cancel,
            )

        self.assertEqual(events.put.call_args_list[0].args[0], ("started",))
        failure = events.put.call_args_list[1].args[0]
        self.assertEqual(failure[0], "failed")
        self.assertEqual(failure[1], "LOCAL_WORKSPACE_STORAGE_IO_FAILED")
        self.assertIn("No Odoo records were changed", failure[2])
        self.assertIn("free space", failure[2])


if __name__ == "__main__":
    unittest.main()
