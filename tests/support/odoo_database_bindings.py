"""Admit explicit typed database proof in isolated transfer test fixtures."""

from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    database_scope_hash,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import WorkspaceState


def reviewed_destination(workspace: WorkspaceState) -> WorkspaceState:
    """Model a deliberate fresh destination review, never runtime admission."""

    scope = database_scope_hash(
        workspace.destination_odoo_base_url, workspace.destination_odoo_database
    )
    generation = OdooDatabaseGeneration("impodo-local", scope, 0)
    binding = OdooDatabaseBinding(
        workspace_id=workspace.workspace_id,
        project_id=str(uuid5(NAMESPACE_URL, f"test-project:{workspace.workspace_id}")),
        organization_id=generation.organization_id,
        role=OdooDatabaseBindingRole.DESTINATION,
        scope_hash=scope,
        generation_hash=generation.generation_hash,
        actor=LOCAL_ACTOR.identity,
        bound_at=workspace.destination_verified_at or workspace.created_at,
        operation_id=str(
            uuid5(UUID(workspace.workspace_id), f"destination-review:{scope}")
        ),
    )
    return replace(
        workspace,
        destination_database_binding=binding,
        destination_database_generation_hash=generation.generation_hash,
    )
