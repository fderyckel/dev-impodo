from __future__ import annotations

from threading import Event
import time
import unittest

from impodo.application.odoo_read_failures import OdooReadFailureCode
from impodo.application.preflight_jobs import (
    PreflightJobManager,
    PreflightJobResult,
    PreflightPhase,
)
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    current_workspace_access_context,
)
from impodo.domain.odoo.contracts import ConnectorTransportError


PROJECT_ID = "10000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
DATA_VERSION_ID = "30000000-0000-4000-8000-000000000001"
MIGRATION_RUN_ID = "40000000-0000-4000-8000-000000000001"


def _access_context() -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        project_id=PROJECT_ID,
        workspace_id=WORKSPACE_ID,
        data_version_id=DATA_VERSION_ID,
        migration_run_id=MIGRATION_RUN_ID,
    )


def _wait_for_terminal(manager: PreflightJobManager, job_id: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = manager.get(WORKSPACE_ID, job_id)
        if job.terminal:
            return job
        time.sleep(0.005)
    raise AssertionError("comparison job did not finish")


class PreflightJobManagerTests(unittest.TestCase):
    def test_reuses_one_active_attempt_and_binds_workspace_access(self) -> None:
        manager = PreflightJobManager()
        started = Event()
        release = Event()
        phases = []

        def work(progress):
            self.assertEqual(current_workspace_access_context(), _access_context())
            started.set()
            release.wait(timeout=1)
            progress(PreflightPhase.READING)
            phases.append(PreflightPhase.READING)
            return PreflightJobResult(
                preflight_run_id="50000000-0000-4000-8000-000000000001",
                redirect_url=f"/workspaces/{WORKSPACE_ID}/summary",
                completion_message="Comparison saved.",
            )

        first = manager.enqueue(
            WORKSPACE_ID,
            "Products",
            access_context=_access_context(),
            work=work,
        )
        self.assertTrue(started.wait(timeout=1))
        repeated = manager.enqueue(
            WORKSPACE_ID,
            "Products",
            access_context=_access_context(),
            work=work,
        )
        release.set()
        finished = _wait_for_terminal(manager, first.job_id)
        manager.shutdown()

        self.assertEqual(first.job_id, repeated.job_id)
        self.assertEqual(finished.status.value, "SUCCEEDED")
        self.assertEqual(finished.progress_percent, 100)
        self.assertEqual(phases, [PreflightPhase.READING])

    def test_classifies_failure_without_exposing_transport_text(self) -> None:
        manager = PreflightJobManager()

        def work(_progress):
            raise ConnectorTransportError("HTTP 503 raw target detail")

        job = manager.enqueue(
            WORKSPACE_ID,
            "Products",
            access_context=_access_context(),
            work=work,
        )
        finished = _wait_for_terminal(manager, job.job_id)
        manager.shutdown()

        self.assertEqual(finished.status.value, "FAILED")
        self.assertIsNotNone(finished.failure)
        assert finished.failure is not None
        self.assertEqual(
            finished.failure.code,
            OdooReadFailureCode.TARGET_UNREACHABLE,
        )
        self.assertEqual(finished.failure.support_code, "ODOO_API_HTTP_503")
        self.assertNotIn("raw target detail", repr(finished))


if __name__ == "__main__":
    unittest.main()
