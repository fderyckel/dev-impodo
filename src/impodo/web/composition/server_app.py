"""Declare the fail-closed shared-server application composition boundary."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI

from impodo.application.project.ports import ScopedMigrationProjectQuery
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.shared.coordination import (
    OperationJournal,
    WorkspaceLeaseRepository,
)
from impodo.application.shared.jobs import JobDispatcher
from impodo.application.shared.secrets import SecretStore
from impodo.application.workspace.access import ScopedWorkspaceAccessQuery
from impodo.domain.shared.access import AuthorizationPolicy
from impodo.web.request_identity import RequestIdentityResolver


class ServerCompositionError(RuntimeError):
    """Report that required shared-server adapters are not yet assembled."""


@dataclass(frozen=True, slots=True)
class ServerAdapterBundle:
    """Name the server-owned ports that may never fall back to local state.

    PostgreSQL implementations will provide identity, authorization, Project
    visibility, Workspace lineage, operation journaling, leases, and durable
    job coordination. The artifact and secret ports may use deployment-chosen
    providers, but they must be explicitly supplied by the server entry point.
    """

    identity_resolver: RequestIdentityResolver
    authorization: AuthorizationPolicy
    project_queries: ScopedMigrationProjectQuery
    workspace_access: ScopedWorkspaceAccessQuery
    operation_journal: OperationJournal
    workspace_leases: WorkspaceLeaseRepository
    artifacts: GovernedArtifactStores
    jobs: JobDispatcher
    secret_store: SecretStore


def create_server_app(
    adapters: ServerAdapterBundle | None = None,
) -> FastAPI:
    """Refuse startup until the shared graph can consume every server port."""

    if adapters is None:
        raise ServerCompositionError(
            "The shared-server composition requires an explicit "
            "ServerAdapterBundle. PostgreSQL control-plane identity, "
            "authorization, Project visibility, Workspace access, "
            "coordination, job, artifact, and secret adapters must be "
            "configured; local fallbacks are forbidden."
        )
    raise ServerCompositionError(
        "The server adapters were supplied, but the deployment-neutral service "
        "graph has not been connected to them yet. Shared-server startup stays "
        "disabled until that composition is complete."
    )


__all__ = [
    "ServerAdapterBundle",
    "ServerCompositionError",
    "create_server_app",
]
