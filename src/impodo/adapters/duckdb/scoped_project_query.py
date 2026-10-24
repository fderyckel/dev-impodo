"""Expose local Project records through the Organization-scoped query port."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from impodo.application.project.ports import MigrationProjectRepository
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    MigrationNotFoundError,
    require_uuid,
)
from impodo.domain.project.models import MigrationProject, MigrationProjectSummary
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    RequestOrganizationScope,
)


class LocalScopedMigrationProjectQuery:
    """Adapt the single-Organization local registry to the scoped query port."""

    def __init__(
        self,
        repository: MigrationProjectRepository,
        *,
        organization_id: OrganizationId,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._organization_id = organization_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_visible_project_summaries(
        self,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> tuple[MigrationProjectSummary, ...]:
        """Return all local Projects only for the implicit local scope."""

        if not self._allows(scope, authorization):
            raise AuthorizationError("Project list is not authorized")
        return self._repository.list_project_summaries()

    def get_visible_project(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        capability: Capability = Capability.PROJECT_VIEW,
    ) -> MigrationProject:
        """Return a local Project without disclosing denied Project metadata."""

        try:
            project_id = require_uuid(project_id, "project_id")
        except MigrationFoundationError as error:
            raise MigrationNotFoundError("Project not found") from error
        if not self._allows(
            scope,
            authorization,
            capability=capability,
            project_id=project_id,
        ):
            raise MigrationNotFoundError("Project not found")
        return self._repository.get_project(project_id)

    def _allows(
        self,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        *,
        capability: Capability = Capability.PROJECT_VIEW,
        project_id: str | None = None,
    ) -> bool:
        return (
            scope.organization_id == self._organization_id
            and authorization.organization_id == self._organization_id
            and scope.principal_id == authorization.principal_id
            and authorization.authorizes(
                capability,
                at=self._clock(),
                project_id=project_id,
            )
        )
