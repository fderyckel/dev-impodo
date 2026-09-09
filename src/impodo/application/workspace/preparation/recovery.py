"""Recover navigation from current published preparation, without scanning rows."""

from dataclasses import dataclass
from typing import Protocol

from impodo.application.workspace.mapping.service import MappingWorkspaceService
from impodo.domain.preparation.quality import retention_context_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.workbench import WorkspaceState
from .job_models import PreparationJobStatus


@dataclass(frozen=True, slots=True)
class PreparationRecoveryInputs:
    """Current dependencies required before a saved result can be resumed."""

    mapping_hash: str
    physical_selection_hash: str
    source_selection_hash: str
    schema_hash: str
    retention_context_hash: str


@dataclass(frozen=True, slots=True)
class RecoveredPreparation:
    """A published review destination, never load or qualification authority."""

    mapping_content_hash: str
    staging_run_id: str
    result_run_id: str
    status: PreparationJobStatus

    def __post_init__(self) -> None:
        if self.status not in {
            PreparationJobStatus.SUCCEEDED, PreparationJobStatus.REVIEW_REQUIRED,
        }:
            raise ValueError("Only a published review result can be recovered")


class PreparationRecoveryRepository(Protocol):
    """Read current evidence bindings together in one workspace snapshot."""

    def read_current(
        self, workspace_id: str, inputs: PreparationRecoveryInputs,
    ) -> RecoveredPreparation | None: ...


class WorkspaceStateReader(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class PreparationRecoveryService:
    """Verify authoritative inputs before reading the compact publication chain."""

    def __init__(
        self, repository: PreparationRecoveryRepository,
        mappings: MappingWorkspaceService, workspace_states: WorkspaceStateReader,
    ) -> None:
        self.repository = repository
        self.mappings = mappings
        self.workspace_states = workspace_states

    def current(
        self, workspace_id: str, mapping_hash: str | None, *, actor: Actor,
    ) -> RecoveredPreparation | None:
        """Recover only the requested Recipe mapping over its current frozen data."""

        self.mappings.authorization.require(actor, Capability.MAPPING_SUBMIT, workspace_id=workspace_id)
        if mapping_hash is None:
            return None
        physical = self.mappings.sources.get_source_selection(workspace_id)
        selection = self.mappings.sources.get_mapping_source_selection(workspace_id)
        schema = self.mappings.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.mappings.schemas.get_schema_governance(workspace_id)
        if physical is None or selection is None or schema is None:
            return None
        if governance is not None and governance.catalog_hash != schema.content_hash:
            return None
        inputs = PreparationRecoveryInputs(
            mapping_hash=mapping_hash,
            physical_selection_hash=physical.content_hash,
            source_selection_hash=selection.content_hash,
            schema_hash=governance.content_hash if governance else schema.content_hash,
            retention_context_hash=retention_context_hash(self.workspace_states.get(workspace_id)),
        )
        return self.repository.read_current(workspace_id, inputs)
