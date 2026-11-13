"""Open reviewed identity aliases inside one authorized local operation."""

from __future__ import annotations

from impodo.domain.mapping.contracts import ValueMapping
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.context import WebContext

from .page_reads import run_local_operation_sync


def read_destination_identity_aliases(
    context: WebContext,
    workspace_id: str,
    plan: DestinationMatchPlan,
    *,
    actor: Actor,
) -> dict[str, tuple[ValueMapping, ...]]:
    """Reopen exact per-run rules before a fresh check, without target I/O."""

    if plan.identity_alias_evidence_id is None:
        return {}
    if plan.workspace_id != workspace_id:
        raise WorkspaceError("The destination value mappings belong to another workspace")

    def read() -> dict[str, tuple[ValueMapping, ...]]:
        access = context.workspace_access.require(
            actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=workspace_id
        )
        evidence = context.destination_identity_aliases.read(access.project_id, plan)
        if evidence is None:
            raise WorkspaceError(
                "The reviewed destination value mappings are unavailable; check matching again"
            )
        return {item.dataset_id: item.aliases for item in evidence.values}

    return run_local_operation_sync(read)
