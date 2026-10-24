"""Expose local Workspace lineage through the Organization-scoped query port."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
)
from impodo.domain.project.foundation import MigrationIdentifierConfusionError
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    RequestOrganizationScope,
)


class LocalScopedWorkspaceAccessQuery:
    """Adapt local Workspace lineage to one implicit Organization scope."""

    def __init__(
        self,
        access: WorkspaceAccessService,
        *,
        organization_id: OrganizationId,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._access = access
        self._organization_id = organization_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve_visible_workspace_access(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        capability: Capability,
    ) -> WorkspaceAccessContext:
        """Return verified local lineage or one non-disclosing denial."""

        capability = Capability(capability)
        if not self._request_allows(scope, authorization, capability):
            raise MigrationIdentifierConfusionError("Workspace not found")
        context = self._access.resolve(
            workspace_id,
            actor=actor,
            capability=capability,
        )
        if not authorization.authorizes(
            capability,
            at=self._clock(),
            project_id=context.project_id,
        ):
            raise MigrationIdentifierConfusionError("Workspace not found")
        return context

    def _request_allows(
        self,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        capability: Capability,
    ) -> bool:
        return (
            scope.organization_id == self._organization_id
            and authorization.organization_id == self._organization_id
            and scope.principal_id == authorization.principal_id
            and authorization.authorizes(capability, at=self._clock())
        )
