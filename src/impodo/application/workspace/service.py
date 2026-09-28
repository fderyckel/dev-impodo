"""Authorize and coordinate isolated workspace lifecycle commands."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from ...domain.serialization import content_hash
from ...domain.workspace.models import (
    MigrationWorkspace,
    MigrationWorkspaceSetupState,
    MigrationWorkspaceState,
)
from impodo.domain.project.foundation import (
    FaultInjector,
    MigrationConflictError,
    require_revision,
    require_uuid,
    utc_now,
)
from .ports import MigrationWorkspaceRepository


class MigrationWorkspaceService:
    """Authorize and coordinate isolated workspace lifecycle commands."""

    def __init__(
        self,
        repository: MigrationWorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def create(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_workspace_revision: int,
        data_version_id: str,
        migration_run_id: str,
        display_name: str,
        recipe_application_id: str | None = None,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> MigrationWorkspace:
        """Create the workspace root for an already-authorized project command.

        Called by project-authoring and production-cutover orchestration; web
        routes reach it through those application services rather than writing
        a workspace directly.  This boundary owns authorization, identifier
        validation, lifecycle defaults, and the idempotency payload.  The
        repository owns durable persistence and event publication.
        """
        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_CREATE,
            project_id=project_id,
        )
        application_id = (
            require_uuid(recipe_application_id, "recipe_application_id")
            if recipe_application_id is not None
            else None
        )
        now = utc_now()
        workspace = MigrationWorkspace(
            workspace_id=str(uuid4()),
            project_id=project_id,
            data_version_id=data_version_id,
            migration_run_id=migration_run_id,
            recipe_application_id=application_id,
            display_name=display_name,
            state=MigrationWorkspaceState.OPEN,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        request_hash = content_hash(
            {
                "data_version_id": data_version_id,
                "display_name": workspace.display_name,
                "migration_run_id": migration_run_id,
                "project_id": project_id,
                "recipe_application_id": application_id,
            }
        )
        return self.repository.create_migration_workspace(
            workspace,
            expected_workspace_revision=require_revision(
                expected_workspace_revision,
                "expected_workspace_revision",
            ),
            operation_id=operation_id or str(uuid4()),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

    def close(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> MigrationWorkspace:
        """Close an open workspace after checking its current revision.

        Called by the correction workflow and its browser route.  Closing is
        intentionally a lifecycle command only: it neither deletes workspace
        evidence nor attempts to finish unfinished workflow stages.
        """
        self.authorization.require(actor, Capability.MIGRATION_WORKSPACE_EDIT)
        current = self.repository.get_migration_workspace(
            require_uuid(workspace_id, "workspace_id")
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_EDIT,
            project_id=current.project_id,
        )
        expected_revision = require_revision(expected_revision)
        if current.optimistic_revision != expected_revision:
            raise MigrationConflictError("MigrationWorkspace close revision is stale")
        if current.state is MigrationWorkspaceState.CLOSED:
            return current
        now = utc_now()
        return self.repository.save_migration_workspace(
            replace(
                current,
                state=MigrationWorkspaceState.CLOSED,
                closed_at=now,
                updated_at=now,
            ),
            expected_revision=expected_revision,
            event_type="MIGRATION_WORKSPACE_CLOSED",
            actor=actor,
        )

    def complete_setup(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> MigrationWorkspace:
        """Mark setup ready on the canonical MigrationWorkspace root."""

        self.authorization.require(actor, Capability.MIGRATION_WORKSPACE_EDIT)
        current = self.repository.get_migration_workspace(
            require_uuid(workspace_id, "workspace_id")
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_EDIT,
            project_id=current.project_id,
        )
        if current.state is not MigrationWorkspaceState.OPEN:
            raise ValueError("A closed MigrationWorkspace cannot complete setup")
        expected_revision = require_revision(expected_revision)
        if current.optimistic_revision != expected_revision:
            raise ValueError("MigrationWorkspace setup revision is stale")
        if current.setup_state is MigrationWorkspaceSetupState.READY:
            return current
        now = utc_now()
        return self.repository.save_migration_workspace(
            replace(
                current,
                setup_state=MigrationWorkspaceSetupState.READY,
                setup_completed_at=now,
                updated_at=now,
            ),
            expected_revision=expected_revision,
            event_type="MIGRATION_WORKSPACE_SETUP_COMPLETED",
            actor=actor,
        )

    def get(self, workspace_id: str, *, actor: Actor) -> MigrationWorkspace:
        """Read one workspace after resolving and authorizing its project.

        This is the narrow lifecycle read used by application orchestration.
        Page composition should use :class:`WorkspaceOwnerViewService` when it
        needs the complete verified owner lineage.
        """
        self.authorization.require(actor, Capability.PROJECT_VIEW)
        workspace = self.repository.get_migration_workspace(
            require_uuid(workspace_id, "workspace_id")
        )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=workspace.project_id,
        )
        return workspace

    def list_for_project(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[MigrationWorkspace, ...]:
        """List the project-owned workspaces visible to ``actor``.

        Called by migration-project pages and project setup scenarios.  It
        authorizes the parent project before delegating the unfiltered list to
        the repository; it does not decide which workspace is active.
        """
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.list_project_migration_workspaces(project_id)
