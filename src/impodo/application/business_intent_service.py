"""Record business expectations through authorized, deployment-neutral ports.

Callers own one bounded synchronous operation. The command reads metadata once,
narrows the existing checked mapping, and records intent without Odoo I/O or
changing comparison, approval, journal, or execution evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.odoo.compatibility import OdooOperation
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.business_intent import (
    BusinessIntent,
    BusinessIntentBinding,
    BusinessIntentError,
    BusinessIntentRevision,
)
from impodo.domain.workspace.business_intent_scope import (
    SupportedBusinessScope,
    derive_business_intent_scope,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaOrigin,
    SourceSelection,
)
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState


class BusinessIntentAuthorization(Protocol):
    """Return verified run purpose as well as authority before child evidence reads."""

    def require(
        self, actor: Actor, capability: Capability, *, workspace_id: str
    ) -> WorkspaceAccessContext: ...


class BusinessIntentRepository(Protocol):
    """Read one immutable revision or atomically append the expected next one."""

    def get_business_intent(
        self, workspace_id: str, version: int | None = None
    ) -> BusinessIntentRevision | None: ...

    def save_business_intent(
        self,
        workspace_id: str,
        revision: BusinessIntentRevision,
        *,
        expected_version: int,
        actor: Actor,
    ) -> BusinessIntentRevision: ...


class BusinessIntentWorkspaceReader(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class BusinessIntentSourceReader(Protocol):
    def get_mapping_source_selection(
        self, workspace_id: str
    ) -> SourceSelection | None: ...


class BusinessIntentSchemaReader(Protocol):
    def get_odoo_schema_catalog(
        self, workspace_id: str
    ) -> OdooSchemaCatalog | None: ...

    def get_schema_governance(self, workspace_id: str) -> SchemaGovernance | None: ...


class BusinessIntentMappingReader(Protocol):
    def get_mapping_revision(
        self, workspace_id: str, version: int | None = None
    ) -> MappingRevision | None: ...

    def get_mapping_validation(
        self, workspace_id: str, version: int
    ) -> MappingValidationResult | None: ...


@dataclass(frozen=True, slots=True)
class BusinessIntentInputs:
    """Expose a reviewable technical ceiling without retaining stores or row data."""

    workspace_id: str
    scope: SupportedBusinessScope
    binding: BusinessIntentBinding
    model_labels: tuple[tuple[str, str], ...] = ()

    @property
    def binding_hash(self) -> str:
        return content_hash(asdict(self.binding))


class BusinessIntentService:
    """Keep reusable expectations separate from operational intents and approval."""

    def __init__(
        self,
        *,
        workspaces: BusinessIntentWorkspaceReader,
        sources: BusinessIntentSourceReader,
        schemas: BusinessIntentSchemaReader,
        mappings: BusinessIntentMappingReader,
        repository: BusinessIntentRepository,
        authorization: BusinessIntentAuthorization,
    ) -> None:
        self.workspaces, self.sources, self.schemas = workspaces, sources, schemas
        self.mappings, self.repository, self.authorization = (
            mappings,
            repository,
            authorization,
        )

    def get(
        self, workspace_id: str, *, actor: Actor, version: int | None = None
    ) -> BusinessIntentRevision | None:
        """Read historical or latest expectations without requiring current inputs."""
        self.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=workspace_id
        )
        if version is not None and (type(version) is not int or version < 1):
            raise BusinessIntentError("Business intent version must be positive")
        return self.repository.get_business_intent(workspace_id, version)

    def inputs(self, workspace_id: str, *, actor: Actor) -> BusinessIntentInputs:
        """Authorize before loading one current metadata packet for all models."""
        access = self.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=workspace_id
        )
        return self._inputs(workspace_id, access)

    def _inputs(
        self, workspace_id: str, access: WorkspaceAccessContext
    ) -> BusinessIntentInputs:
        if access.workspace_id != workspace_id or access.run_purpose not in {
            "AUTHORING",
            "TEST",
            "PRODUCTION",
        }:
            raise BusinessIntentError(
                "Verify the workspace's owning run purpose before reviewing business intent"
            )
        workspace = self.workspaces.get(workspace_id)
        if (
            workspace.source_mode is SourceMode.ODOO
            and workspace.destination_configured
        ):
            raise BusinessIntentError(
                "Business intent for a separate Odoo destination requires its destination-owned write scope"
            )
        mapping = self.mappings.get_mapping_revision(workspace_id)
        selection = self.sources.get_mapping_source_selection(workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.schemas.get_schema_governance(workspace_id)
        return checked_business_intent_inputs(
            workspace=workspace,
            mapping=mapping,
            selection=selection,
            schema=schema,
            governance=governance,
            validation=(
                self.mappings.get_mapping_validation(workspace_id, mapping.version)
                if mapping is not None
                else None
            ),
            operation=(
                OdooOperation.PRODUCTION
                if access.run_purpose == "PRODUCTION"
                else OdooOperation.WRITE
            ),
        )

    def save(
        self,
        workspace_id: str,
        *,
        intent: BusinessIntent,
        expected_version: int,
        expected_binding_hash: str,
        operation_id: str,
        actor: Actor,
    ) -> BusinessIntentRevision:
        """Narrow current technical authority and append one explicit revision."""
        access = self.authorization.require(
            actor, Capability.MAPPING_EDIT, workspace_id=workspace_id
        )
        if type(expected_version) is not int or expected_version < 0:
            raise BusinessIntentError(
                "Expected business intent version must be nonnegative"
            )
        operation_id = str(UUID(operation_id))
        if not isinstance(intent, BusinessIntent):
            raise BusinessIntentError(
                "Business intent requires a validated portable contract"
            )
        packet = self._inputs(workspace_id, access)
        if packet.binding_hash != expected_binding_hash:
            raise BusinessIntentError(
                "The business write scope changed; review current intent inputs again"
            )
        packet.scope.require_narrowing(intent)
        revision = BusinessIntentRevision(
            workspace_id,
            expected_version + 1,
            operation_id,
            intent,
            packet.binding,
            actor.identity,
            datetime.now(UTC),
        )
        return self.repository.save_business_intent(
            workspace_id, revision, expected_version=expected_version, actor=actor
        )


def checked_business_intent_inputs(
    *,
    workspace: WorkspaceState,
    mapping: MappingRevision | None,
    selection: SourceSelection | None,
    schema: OdooSchemaCatalog | None,
    governance: SchemaGovernance | None,
    validation: MappingValidationResult | None,
    operation: OdooOperation,
) -> BusinessIntentInputs:
    """Project already-loaded metadata; command callers still authorize and re-read."""
    if (
        mapping is None
        or selection is None
        or schema is None
        or schema.origin is not SchemaOrigin.LIVE_API
    ):
        raise BusinessIntentError(
            "Check the mapping against captured Odoo details before recording business intent"
        )
    schema_hash = governance.content_hash if governance else schema.content_hash
    if (
        mapping.definition.source_selection_hash != selection.content_hash
        or mapping.definition.schema_hash != schema_hash
        or (governance is not None and governance.catalog_hash != schema.content_hash)
        or validation is None
        or validation.status is MappingValidationStatus.INVALID
        or validation.mapping_content_hash != mapping.definition.content_hash
        or validation.source_selection_hash != selection.content_hash
        or validation.schema_hash != schema_hash
    ):
        raise BusinessIntentError(
            "Recheck the current mapping before recording business intent"
        )
    scope = derive_business_intent_scope(
        mapping.definition,
        selection,
        schema,
        source_mode=workspace.source_mode,
        operation=operation,
    )
    return BusinessIntentInputs(
        workspace.workspace_id,
        scope,
        BusinessIntentBinding(
            mapping.definition.content_hash,
            selection.content_hash,
            schema.content_hash,
            governance.content_hash if governance else None,
            validation.validation_hash,
            scope.content_hash,
        ),
        tuple((item.name, item.label or item.name) for item in schema.models),
    )
