"""Keep optional terminal observations from blocking worker reclamation."""

from __future__ import annotations

import tempfile
import unittest
from queue import Empty
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.preparation.job_models import PreparationWorkspace
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.web.composition.preparation_job_manager import PreparationJobManager


class PreparationPerformanceCleanupTests(unittest.TestCase):
    def test_terminal_history_deletion_still_closes_queue_and_schedules_next_job(self):
        workspace = PreparationWorkspace(
            project_id=str(uuid4()),
            data_version_id=str(uuid4()),
            data_version_number=1,
            data_version_purpose=DataVersionPurpose.AUTHORING,
            migration_run_id=str(uuid4()),
            migration_run_purpose=MigrationRunPurpose.AUTHORING,
            workspace_id=str(uuid4()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            manager = PreparationJobManager(temporary)
            with patch.object(manager, "_schedule_locked") as schedule:
                job = manager.enqueue(
                    workspace.workspace_id,
                    "Fictional project",
                    12,
                    actor=LOCAL_ACTOR,
                    workspace=workspace,
                )
                manager._pending.pop(job.job_id)
                manager._workers[job.job_id] = MagicMock()
                manager._performance[job.job_id].start(
                    None, PROCESS_BUILD_CONTRACT.application_build_id
                )
                schedule.reset_mock()
                records = iter([
                    ("started",),
                    ("succeeded", "normalization-run"),
                ])

                def drain():
                    try:
                        return next(records)
                    except StopIteration:
                        manager.delete_workspace_history(workspace.workspace_id)
                        raise Empty from None

                queue = MagicMock()
                queue.get_nowait.side_effect = drain
                process = MagicMock()
                process.is_alive.return_value = False

                manager._supervise(job.job_id, process, queue, LOCAL_ACTOR)

                queue.close.assert_called_once_with()
                self.assertNotIn(job.job_id, manager._workers)
                schedule.assert_called_once_with()
                evidence = manager.performance_evidence(job.job_id)
                self.assertFalse(evidence["measurement_complete"])
                self.assertEqual(evidence["outcome"], "interrupted")
