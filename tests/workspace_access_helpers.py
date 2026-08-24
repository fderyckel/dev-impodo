"""Deterministic, distinct lineage identities for workspace-scoped tests."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from impodo.access import AuthorizationPolicy, CapabilityAuthorizationPolicy
from impodo.workspace_access import WorkspaceAccessContext, WorkspaceAccessService


def lineage_id(kind: str, workspace_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"impodo-test:{kind}:{workspace_id}"))


def data_version_id(workspace_id: str) -> str:
    return lineage_id("data-version", workspace_id)


class DeterministicWorkspaceLineageRepository:
    def resolve_workspace_access_context(
        self,
        workspace_id: str,
    ) -> WorkspaceAccessContext:
        return WorkspaceAccessContext(
            project_id=lineage_id("project", workspace_id),
            workspace_id=workspace_id,
            data_version_id=data_version_id(workspace_id),
            migration_run_id=lineage_id("run", workspace_id),
        )


def workspace_access_service(
    authorization: AuthorizationPolicy | None = None,
) -> WorkspaceAccessService:
    return WorkspaceAccessService(
        DeterministicWorkspaceLineageRepository(),
        authorization or CapabilityAuthorizationPolicy(),
    )
