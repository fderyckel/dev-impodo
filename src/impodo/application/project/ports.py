"""Declare the persistence port consumed by Project application commands."""

from __future__ import annotations

from typing import Protocol

from impodo.domain.project.foundation import FaultInjector
from impodo.domain.shared.access import Actor
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    RequestOrganizationScope,
)

from ...domain.project.models import MigrationProject, MigrationProjectSummary


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

    def delete_project(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> MigrationProject: ...


class ScopedMigrationProjectQuery(Protocol):
    """Return only Projects visible in one fixed Organization request scope."""

    def list_visible_project_summaries(
        self,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> tuple[MigrationProjectSummary, ...]:
        """Filter in the control store before opening customer data stores."""
        ...

    def get_visible_project(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> MigrationProject:
        """Return a visible Project or one non-disclosing not-found result."""
        ...
