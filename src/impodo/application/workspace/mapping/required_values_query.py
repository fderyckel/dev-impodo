"""Own required-value input reads behind a value-free application query port.

Local composition runs this coordinator in its page operation. Server
composition injects a verified worker-projection reader instead; the server web
process never acquires the draft, schema defaults, or validation messages.
"""

from typing import Protocol

from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.validation.evidence import MappingValidationResult
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import MappingWorkingDraft
from impodo.domain.workspace.errors import WorkspaceError

from .order_service import MatchingOrderService
from .required_values import RequiredValuesProjection
from .service import (
    MappingSchemaRepository,
    MappingSourceRepository,
    MappingWorkspaceService,
)


class RequiredValuesQuery(Protocol):
    """Return advisory field metadata; never return literals or default values."""

    def review(self, workspace_id: str, *, actor: Actor) -> RequiredValuesProjection:
        """Read current coverage from local inputs or a verified worker receipt."""
        ...


class RequiredValuesInputsReader(
    MappingSourceRepository, MappingSchemaRepository, Protocol
):
    """Bulk input boundary for the local coordinator or worker, never a server page."""

    def get_mapping_revision(self, workspace_id: str) -> MappingRevision | None: ...

    def get_mapping_working_draft(
        self, workspace_id: str
    ) -> MappingWorkingDraft | None: ...

    def get_mapping_validation(
        self, workspace_id: str, version: int
    ) -> MappingValidationResult | None: ...


class RequiredValuesQueryService:
    """Read each saved evidence owner in bulk and project with existing policy."""

    def __init__(
        self,
        inputs: RequiredValuesInputsReader,
        mapping: MappingWorkspaceService,
        matching: MatchingOrderService,
    ):
        self.inputs, self.mapping, self.matching = inputs, mapping, matching

    def review(self, workspace_id: str, *, actor: Actor) -> RequiredValuesProjection:
        """Authorize before input access, with no scan, capture, or validation."""
        self.mapping.authorization.require(
            actor, Capability.MAPPING_EDIT, workspace_id=workspace_id
        )
        selection = self.inputs.get_mapping_source_selection(workspace_id)
        schema = self.inputs.get_odoo_schema_catalog(workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Choose source and Odoo data before reviewing required values"
            )
        governance = self.inputs.get_schema_governance(workspace_id)
        revision = self.inputs.get_mapping_revision(workspace_id)
        draft = self.inputs.get_mapping_working_draft(workspace_id)
        validation = (
            self.inputs.get_mapping_validation(workspace_id, revision.version)
            if revision
            else None
        )
        check = self.matching.current_check(workspace_id, actor=actor)
        return self.mapping.required_values(
            workspace_id,
            selection,
            schema,
            governance,
            revision,
            draft,
            validation,
            actor=actor,
            live_check=check,
        )
