"""Keep worker measurements correlated without giving them outcome authority."""

from __future__ import annotations

import tempfile
import unittest
from queue import Empty
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import (
    record_duckdb_connection_timing,
    record_duckdb_schema_timing,
)
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.shared.preparation_runtime import (
    PreparationRuntimeConfiguration,
    PreparationRuntimeConfigurationError,
    PreparationThreadSource,
)
from impodo.application.workspace.preparation.job_models import (
    PreparationJobStatus,
    PreparationPhase,
    PreparationWorkspace,
)
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.preparation_job_manager import (
    PreparationCancelled,
    PreparationJobManager,
    _run_preparation_worker,
)

MODULE = "impodo.web.composition.preparation_job_manager"
COMPOSITION = "impodo.web.composition.preparation_worker.create_preparation_worker"


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


class _Events:
    def __init__(self, *, reject_performance: bool = False) -> None:
        self.records = []
        self.reject_performance = reject_performance

    def put(self, event: tuple[object, ...]) -> None:
        if self.reject_performance and event[0] == "performance":
            raise OSError("Optional diagnostic transport unavailable")
        self.records.append(event)


class PreparationWorkerPerformanceIntegrationTests(unittest.TestCase):
    def run_worker(self, *, failure=None, events=None):
        sink = events or _Events()
        workspace = _workspace()
        configuration = PreparationRuntimeConfiguration(
            2, 2, PreparationThreadSource.PREPARATION_OVERRIDE
        )

        def prepare(_workspace_id, *, progress, performance, timing, **_kwargs):
            progress(PreparationPhase.TRANSFORMING, 12, 12, "Preparing")
            performance(
                {
                    "workload": {
                        "row_count": 12,
                        "column_count": 7,
                        "relationship_count": 2,
                        "input_bytes": 2048,
                        "processing_route": "NATIVE_COLUMNAR",
                    }
                }
            )
            record_duckdb_connection_timing(
                duration_ms=3.5, lock_wait_ms=0.5, lock_retry_count=1
            )
            record_duckdb_schema_timing(duration_ms=2.25)
            timing("quality_evaluation", 4.5, "completed")
            if failure is not None:
                raise failure
            return SimpleNamespace(run_id="normalization-run")

        with (
            patch(
                f"{MODULE}.configure_preparation_runtime", return_value=configuration
            ),
            patch(f"{MODULE}.process_time", return_value=1.25),
            patch(
                COMPOSITION,
                return_value=SimpleNamespace(prepare=prepare, resolution=None),
            ),
        ):
            _run_preparation_worker(
                "unused-root",
                workspace.workspace_id,
                workspace,
                PROCESS_BUILD_CONTRACT,
                LOCAL_ACTOR,
                sink,
                SimpleNamespace(is_set=lambda: False),
            )
        return workspace, sink.records

    def test_worker_emits_configuration_workload_and_terminal_database_cpu_counts(
        self,
    ) -> None:
        _workspace_packet, records = self.run_worker()
        measurements = [event[1] for event in records if event[0] == "performance"]
        configuration = measurements[0]["configuration"]
        self.assertEqual(configuration["polars_threads_requested"], 2)
        self.assertEqual(configuration["polars_threads_effective"], 2)
        self.assertEqual(configuration["worker_processes"], 1)
        self.assertEqual(measurements[1]["workload"]["row_count"], 12)
        final = measurements[-1]
        self.assertEqual(final["resources"]["cpu_seconds"], 1.25)
        self.assertEqual(
            final["database"],
            {
                "connection_count": 1,
                "schema_check_count": 1,
                "lock_retry_count": 1,
                "connect_ms": 3.5,
                "schema_ms": 2.25,
                "lock_wait_ms": 0.5,
            },
        )
        self.assertTrue(final["measurement_complete"])
        self.assertEqual(records[-1], ("succeeded", "normalization-run"))
        self.assertEqual(records[-2][0], "performance")

    def test_terminal_metrics_precede_success_cancellation_and_known_failure(
        self,
    ) -> None:
        for failure, terminal in (
            (None, "succeeded"),
            (PreparationCancelled(), "cancelled"),
            (WorkspaceError("Source unavailable"), "failed"),
        ):
            with self.subTest(terminal=terminal):
                _workspace_packet, records = self.run_worker(failure=failure)
                self.assertEqual(records[-1][0], terminal)
                self.assertEqual(records[-2][0], "performance")
                self.assertTrue(records[-2][1]["measurement_complete"])
                total = next(
                    event
                    for event in records
                    if event[0] == "timing" and event[1] == "total"
                )
                self.assertEqual(total[3], terminal)

    def test_invalid_or_unapplied_configuration_stops_before_composition(self) -> None:
        for suffix in ("INVALID", "MISMATCH"):
            code = "PREPARATION_THREAD_CONFIGURATION_" + suffix
            with self.subTest(code=code):
                sink = _Events()
                workspace = _workspace()
                with (
                    patch(
                        f"{MODULE}.configure_preparation_runtime",
                        side_effect=PreparationRuntimeConfigurationError(
                            "Thread configuration could not be verified",
                            failure_code=code,
                        ),
                    ),
                    patch(COMPOSITION) as compose,
                ):
                    _run_preparation_worker(
                        "unused-root",
                        workspace.workspace_id,
                        workspace,
                        PROCESS_BUILD_CONTRACT,
                        LOCAL_ACTOR,
                        sink,
                        MagicMock(),
                    )
                compose.assert_not_called()
                self.assertEqual(sink.records[-1][0:2], ("failed", code))
                self.assertFalse(sink.records[-2][1]["measurement_complete"])

    def test_optional_measurement_transport_failure_keeps_success(self) -> None:
        sink = _Events(reject_performance=True)
        _workspace_packet, records = self.run_worker(events=sink)
        self.assertEqual(records[-1], ("succeeded", "normalization-run"))

    def test_supervisor_correlates_worker_metrics_to_exact_attempt(self) -> None:
        workspace, records = self.run_worker()
        with tempfile.TemporaryDirectory() as temporary:
            recorder = MagicMock()
            manager = PreparationJobManager(temporary, diagnostic_recorder=recorder)
            with patch.object(manager, "_schedule_locked"):
                job = manager.enqueue(
                    workspace.workspace_id,
                    "Fictional project",
                    12,
                    actor=LOCAL_ACTOR,
                    workspace=workspace,
                )
                other_workspace = _workspace()
                other_job = manager.enqueue(
                    other_workspace.workspace_id,
                    "Other fictional project",
                    30,
                    actor=LOCAL_ACTOR,
                    workspace=other_workspace,
                )
                manager._performance[job.job_id].start(
                    None, PROCESS_BUILD_CONTRACT.application_build_id
                )
                queue = MagicMock()
                queue.get_nowait.side_effect = [*records, Empty()]
                process = MagicMock()
                process.is_alive.return_value = False
                manager._supervise(job.job_id, process, queue, LOCAL_ACTOR)

            self.assertEqual(
                manager.get(workspace.workspace_id, job.job_id).status,
                PreparationJobStatus.SUCCEEDED,
            )
            evidence = manager.performance_evidence(job.job_id)
            self.assertEqual(evidence["attempt_id"], job.job_id)
            self.assertEqual(evidence["workload"]["row_count"], 12)
            self.assertEqual(evidence["workload"]["column_count"], 7)
            self.assertEqual(evidence["configuration"]["polars_threads_effective"], 2)
            self.assertEqual(evidence["database"]["connection_count"], 1)
            self.assertTrue(evidence["measurement_complete"])
            self.assertEqual(
                manager.performance_evidence(other_job.job_id)["workload"]["row_count"],
                30,
            )
            self.assertTrue(
                all(
                    call.args[0]["attempt_id"] == job.job_id
                    for call in recorder.record_performance_event.call_args_list
                )
            )

    def test_recorder_failure_keeps_saved_job_outcome_and_marks_measurement_incomplete(
        self,
    ) -> None:
        workspace, records = self.run_worker()
        with tempfile.TemporaryDirectory() as temporary:
            recorder = MagicMock()
            recorder.record_performance_event.side_effect = OSError("Disk unavailable")
            manager = PreparationJobManager(temporary, diagnostic_recorder=recorder)
            with patch.object(manager, "_schedule_locked"):
                job = manager.enqueue(
                    workspace.workspace_id,
                    "Fictional project",
                    12,
                    actor=LOCAL_ACTOR,
                    workspace=workspace,
                )
                manager._performance[job.job_id].start(
                    None, PROCESS_BUILD_CONTRACT.application_build_id
                )
                queue = MagicMock()
                queue.get_nowait.side_effect = [*records, Empty()]
                process = MagicMock()
                process.is_alive.return_value = False
                manager._supervise(job.job_id, process, queue, LOCAL_ACTOR)

            self.assertEqual(
                manager.get(workspace.workspace_id, job.job_id).status,
                PreparationJobStatus.SUCCEEDED,
            )
            self.assertFalse(
                manager.performance_evidence(job.job_id)["measurement_complete"]
            )


if __name__ == "__main__":
    unittest.main()
