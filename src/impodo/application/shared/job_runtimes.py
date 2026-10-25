"""Deployment-neutral control-plane ports for browser-managed background work.

The local application satisfies these protocols with in-process managers. A
server deployment may instead use durable PostgreSQL job records and external
workers while presenting the same route-facing lifecycle.
"""

from __future__ import annotations

from typing import Protocol

from impodo.application.correction_jobs import (
    CorrectionJob,
    CorrectionJobKind,
    CorrectionWork,
)
from impodo.application.odoo_source_capture_service import OdooSourceCapturePort
from impodo.application.preflight_jobs import (
    PreflightJob,
    PreflightWork,
)
from impodo.application.run.recipe_run_jobs import (
    RecipeRunJob,
    RecipeRunJobKind,
    RecipeRunWork,
)
from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.application.workspace.execution.job_models import LoadJob
from impodo.application.workspace.execution.load_jobs import LoadWork
from impodo.application.workspace.odoo_capture_jobs import OdooCaptureJob
from impodo.application.workspace.preparation.job_models import (
    PreparationJob,
    PreparationWorkspace,
)
from impodo.application.workspace.preparation.recovery import RecoveredPreparation
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.shared.access import Actor


class RecipeRunJobRuntime(Protocol):
    """Schedule and inspect long-running Recipe checks for browser routes."""

    def enqueue(
        self,
        *,
        kind: RecipeRunJobKind,
        project_id: str,
        migration_run_id: str,
        workspace_id: str,
        application_id: str = "",
        run_purpose: str = "",
        work: RecipeRunWork,
    ) -> RecipeRunJob: ...

    def get(
        self,
        project_id: str,
        migration_run_id: str,
        job_id: str,
    ) -> RecipeRunJob: ...

    def shutdown(self) -> None: ...


class PreparationJobRuntime(Protocol):
    """Schedule, recover, and inspect preparation work for browser routes."""

    def restore_result(
        self,
        workspace: PreparationWorkspace,
        result: RecoveredPreparation,
        *,
        actor: Actor,
        migration_project_name: str,
        expected_job_id: str | None,
    ) -> PreparationJob | None: ...

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        total_rows: int,
        *,
        actor: Actor,
        workspace: PreparationWorkspace,
    ) -> PreparationJob: ...

    def retry(
        self,
        workspace_id: str,
        job_id: str,
        migration_project_name: str,
        total_rows: int,
        *,
        actor: Actor,
        workspace: PreparationWorkspace,
    ) -> PreparationJob: ...

    def get(self, workspace_id: str, job_id: str) -> PreparationJob: ...

    def active(self, workspace_id: str) -> PreparationJob | None: ...

    def latest_many(
        self,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, PreparationJob]: ...

    def delete_workspace_history(self, workspace_id: str) -> None: ...

    def cancel(self, workspace_id: str, job_id: str) -> PreparationJob: ...

    def shutdown(self) -> None: ...


class PreflightJobRuntime(Protocol):
    """Schedule and inspect read-only Odoo comparisons for browser routes."""

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        *,
        access_context: WorkspaceAccessContext,
        work: PreflightWork,
    ) -> PreflightJob: ...

    def get(self, workspace_id: str, job_id: str) -> PreflightJob: ...

    def active(self, workspace_id: str) -> PreflightJob | None: ...

    def shutdown(self) -> None: ...


class LoadJobRuntime(Protocol):
    """Schedule and inspect governed Odoo loads for browser routes."""

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        *,
        target_database: str,
        target_server: str,
        target_environment: str,
        total_rows: int,
        relationship_total_rows: int = 0,
        load_group_count: int = 0,
        recovery_run: ExecutionRun | None = None,
        access_context: WorkspaceAccessContext,
        actor: Actor,
        work: LoadWork,
    ) -> LoadJob: ...

    def get(self, workspace_id: str, job_id: str) -> LoadJob: ...

    def active(self, workspace_id: str) -> LoadJob | None: ...

    def latest(self, workspace_id: str) -> LoadJob | None: ...

    def latest_many(self, workspace_ids: tuple[str, ...]) -> dict[str, LoadJob]: ...

    def shutdown(self) -> None: ...


class CorrectionJobRuntime(Protocol):
    """Schedule and inspect completed-load correction work for browser routes."""

    def enqueue(
        self,
        completed_workspace_id: str,
        successor_workspace_id: str,
        *,
        kind: CorrectionJobKind,
        work: CorrectionWork,
    ) -> CorrectionJob: ...

    def get(self, completed_workspace_id: str, job_id: str) -> CorrectionJob: ...

    def active(self, completed_workspace_id: str) -> CorrectionJob | None: ...

    def latest(self, completed_workspace_id: str) -> CorrectionJob | None: ...

    def shutdown(self) -> None: ...


class OdooCaptureJobRuntime(Protocol):
    """Schedule, stop, and inspect Odoo source captures for browser routes."""

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        maximum_rows: int,
        gateway: OdooSourceCapturePort,
        *,
        access_context: WorkspaceAccessContext,
        actor: Actor,
    ) -> OdooCaptureJob: ...

    def get(self, workspace_id: str, job_id: str) -> OdooCaptureJob: ...

    def active(self, workspace_id: str) -> OdooCaptureJob | None: ...

    def cancel(self, workspace_id: str, job_id: str) -> OdooCaptureJob: ...

    def shutdown(self) -> None: ...


__all__ = [
    "CorrectionJobRuntime",
    "LoadJobRuntime",
    "OdooCaptureJobRuntime",
    "PreflightJobRuntime",
    "PreparationJobRuntime",
    "RecipeRunJobRuntime",
]
