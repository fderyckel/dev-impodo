"""Declare the persistence port consumed by Project application commands."""

from __future__ import annotations

from typing import Protocol

from ...access import Actor
from ...domain.project.models import MigrationProject, MigrationProjectSummary
from ...migration_foundation import FaultInjector


class MigrationProjectRepository(Protocol):
    """Persist Project roots without exposing a storage implementation."""

    def create_project(
        self,
        project: MigrationProject,
        *,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationProject: ...

    def get_project(self, project_id: str) -> MigrationProject: ...

    def list_project_summaries(self) -> tuple[MigrationProjectSummary, ...]: ...

    def save_project(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationProject: ...
