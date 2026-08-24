"""Resolve one workspace to its verified Project-owned access context.

The access contract is deliberately read-only. It resolves lineage from the
registry before a caller opens a workspace store, a DataVersion store,
protected evidence, credentials, or an Odoo connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from .access import Actor, AuthorizationPolicy, Capability
from .migration_foundation import (
    MigrationIdentifierConfusionError,
    require_uuid,
)


@dataclass(frozen=True, slots=True)
class WorkspaceAccessContext:
    """Verified lineage and authorization scope for one MigrationWorkspace."""

    project_id: str
    workspace_id: str
    data_version_id: str
    migration_run_id: str
    recipe_application_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.project_id, "project_id"),
            (self.workspace_id, "workspace_id"),
            (self.data_version_id, "data_version_id"),
            (self.migration_run_id, "migration_run_id"),
        ):
            require_uuid(value, name)
        if self.recipe_application_id is not None:
            require_uuid(self.recipe_application_id, "recipe_application_id")


_BOUND_WORKSPACE_ACCESS: ContextVar[WorkspaceAccessContext | None] = ContextVar(
    "impodo_workspace_access",
    default=None,
)


@contextmanager
def bind_workspace_access_context(
    context: WorkspaceAccessContext,
) -> Iterator[WorkspaceAccessContext]:
    """Reuse one verified lineage packet within a request or worker command."""

    current = _BOUND_WORKSPACE_ACCESS.get()
    if current is not None and current != context:
        raise MigrationIdentifierConfusionError(
            "A command cannot change its authorized workspace context"
        )
    token = _BOUND_WORKSPACE_ACCESS.set(context)
    try:
        yield context
    finally:
        _BOUND_WORKSPACE_ACCESS.reset(token)


def current_workspace_access_context() -> WorkspaceAccessContext | None:
    """Return the already verified lineage bound to the current command."""

    return _BOUND_WORKSPACE_ACCESS.get()


class WorkspaceAccessRepository(Protocol):
    """Return workspace lineage from one bounded registry read."""

    def resolve_workspace_access_context(
        self,
        workspace_id: str,
    ) -> WorkspaceAccessContext: ...


class WorkspaceAccessService:
    """Authorize one workspace request against its actual parent Project."""

    def __init__(
        self,
        repository: WorkspaceAccessRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def resolve(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        capability: Capability,
    ) -> WorkspaceAccessContext:
        """Return verified lineage without opening any child or external store."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        capability = Capability(capability)
        self.authorization.require(actor, capability)
        context = _BOUND_WORKSPACE_ACCESS.get()
        if context is None:
            context = self.repository.resolve_workspace_access_context(workspace_id)
        elif context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "A command cannot change its authorized workspace identity"
            )
        if context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "Workspace access resolver returned a different identity"
            )
        self.authorization.require(
            actor,
            capability,
            project_id=context.project_id,
        )
        return context

    def require(
        self,
        actor: Actor,
        capability: Capability,
        *,
        workspace_id: str,
    ) -> WorkspaceAccessContext:
        """Authorize one exact workspace and return its verified lineage."""

        return self.resolve(
            workspace_id,
            actor=actor,
            capability=Capability(capability),
        )
