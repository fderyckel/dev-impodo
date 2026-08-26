"""Authorize and orchestrate one closed live Odoo-source page stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from impodo.domain.shared.access import Actor, Capability
from impodo.domain.odoo.contracts import MetadataSnapshot
from ..domain.odoo_capture import OdooCaptureSelection
from ..domain.odoo_source_capture import (
    CancellationProbe,
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureSample,
    OdooSourceCaptureConsistencyError,
    OdooSourceCaptureRequest,
    plan_odoo_source_capture,
    require_not_cancelled,
)
from impodo.domain.shared.models import FieldMetadata, OdooReadIdentity, ProtectedOdooReadContext
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus, SourceMode
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaField, SchemaOrigin
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.workspace.access import WorkspaceAccessService


class OdooCaptureWorkspaceReader(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class OdooCaptureSelectionReader(Protocol):
    def get_current_odoo_capture_selection(
        self,
        workspace_id: str,
    ) -> OdooCaptureSelection | None: ...


class OdooCaptureSchemaReader(Protocol):
    def get_odoo_schema_catalog(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None: ...


class OdooSourceCaptureSession(Protocol):
    def pages(self): ...

    @property
    def accounting(self) -> OdooCaptureAccounting: ...


class OdooSourceCapturePort(Protocol):
    """Closed adapter surface; there is no raw domain/method/context call."""

    def probe_identity(
        self,
        request: OdooSourceCaptureRequest,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[OdooReadIdentity, ProtectedOdooReadContext]: ...

    def probe_schema(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> MetadataSnapshot: ...

    def open_capture(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> OdooSourceCaptureSession: ...

    def sample(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        limit: int,
        cancellation: CancellationProbe | None = None,
    ) -> OdooCaptureSample: ...


@dataclass(frozen=True, slots=True)
class OdooSourceCaptureResult:
    """Completed read accounting; Phase 4 owns artifact publication."""

    request: OdooSourceCaptureRequest
    selection: OdooCaptureSelection
    accounting: OdooCaptureAccounting


class OdooSourceCaptureService:
    """Keep selection/schema/identity stable around one validated page stream."""

    def __init__(
        self,
        workspaces: OdooCaptureWorkspaceReader,
        selections: OdooCaptureSelectionReader,
        schemas: OdooCaptureSchemaReader,
        authorization: WorkspaceAccessService,
    ) -> None:
        self._workspaces = workspaces
        self._selections = selections
        self._schemas = schemas
        self._authorization = authorization

    def capture(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        consume_page_factory: Callable[
            [OdooSourceCaptureRequest, OdooCaptureSelection],
            Callable[[OdooCapturePage], None],
        ],
        actor: Actor,
        cancellation: CancellationProbe | None = None,
    ) -> OdooSourceCaptureResult:
        """Validate at both ends and pass each bounded typed page to one sink."""

        request, schema, selection = self._context(workspace_id, actor=actor)
        consume_page = consume_page_factory(request, selection)
        require_not_cancelled(cancellation)
        identity, protected_context = gateway.probe_identity(
            request,
            cancellation=cancellation,
        )
        _require_identity(request, identity)
        _require_live_schema(
            request,
            schema,
            gateway.probe_schema(
                request,
                protected_context,
                cancellation=cancellation,
            ),
        )
        session = gateway.open_capture(
            request,
            protected_context,
            cancellation=cancellation,
        )
        for page in session.pages():
            require_not_cancelled(cancellation)
            consume_page(page)
        accounting = session.accounting
        require_not_cancelled(cancellation)

        # The protected repository pointers and the target must both remain
        # unchanged. These are bounded control-plane checks, not row hashes.
        current_selection = self._selections.get_current_odoo_capture_selection(
            workspace_id
        )
        current_schema = self._schemas.get_odoo_schema_catalog(workspace_id)
        if (
            current_selection is None
            or current_selection.content_hash != request.selection_hash
            or current_schema is None
            or current_schema.content_hash != request.expected_schema_scope_hash
        ):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture selection or schema changed during capture"
            )
        end_identity, end_context = gateway.probe_identity(
            request,
            cancellation=cancellation,
        )
        _require_identity(request, end_identity)
        if end_context != protected_context:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture company or locale context changed during capture"
            )
        _require_live_schema(
            request,
            schema,
            gateway.probe_schema(
                request,
                end_context,
                cancellation=cancellation,
            ),
        )
        require_not_cancelled(cancellation)
        return OdooSourceCaptureResult(
            request=request,
            selection=selection,
            accounting=accounting,
        )

    def sample(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        limit: int,
        actor: Actor,
        cancellation: CancellationProbe | None = None,
    ) -> OdooCaptureSample:
        """Return a bounded, explicitly non-authoritative sample."""

        request, schema, _ = self._context(workspace_id, actor=actor)
        identity, protected_context = gateway.probe_identity(
            request,
            cancellation=cancellation,
        )
        _require_identity(request, identity)
        _require_live_schema(
            request,
            schema,
            gateway.probe_schema(
                request,
                protected_context,
                cancellation=cancellation,
            ),
        )
        result = gateway.sample(
            request,
            protected_context,
            limit=limit,
            cancellation=cancellation,
        )
        if not result.non_authoritative:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture sample claimed authoritative membership"
            )
        return result

    def _context(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[
        OdooSourceCaptureRequest,
        OdooSchemaCatalog,
        OdooCaptureSelection,
    ]:
        context = self._authorization.require(
            actor,
            Capability.SOURCE_CAPTURE,
            workspace_id=workspace_id,
        )
        workspace_state = self._workspaces.get(workspace_id)
        if (
            workspace_state.status is not WorkspaceStatus.REGISTERED
            or workspace_state.source_mode is not SourceMode.ODOO
        ):
            raise WorkspaceError(
                "Live Odoo source capture requires a registered Odoo-source workspace"
            )
        selection = self._selections.get_current_odoo_capture_selection(workspace_id)
        schema = self._schemas.get_odoo_schema_catalog(workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Save a current Odoo capture selection and live schema first"
            )
        if selection.data_version_id != context.data_version_id:
            raise WorkspaceError(
                "The Odoo capture selection belongs to another DataVersion"
            )
        if schema.origin is not SchemaOrigin.LIVE_API:
            raise WorkspaceError(
                "Live Odoo source capture requires authenticated schema evidence"
            )
        return plan_odoo_source_capture(selection, schema), schema, selection


def _require_identity(
    request: OdooSourceCaptureRequest,
    identity: OdooReadIdentity,
) -> None:
    if (
        identity.target_hash != request.expected_connection_target_hash
        or identity.principal_hash != request.expected_read_principal_hash
        or identity.permission_hash != request.expected_read_permission_hash
        or identity.context_hash != request.expected_context_hash
        or identity.readable_models != request.schema_model_names
    ):
        raise OdooSourceCaptureConsistencyError(
            "Odoo capture target, principal, permission, or context changed"
        )


def _require_live_schema(
    request: OdooSourceCaptureRequest,
    stored: OdooSchemaCatalog,
    live: MetadataSnapshot,
) -> None:
    if (
        not live.complete
        or live.fingerprint.target_hash != request.expected_connection_target_hash
        or live.fingerprint.connection_mode != stored.connection_mode
        or live.fingerprint.database != stored.database
        or not live.fingerprint.odoo_version.startswith("19.")
        or set(live.models) != set(request.schema_model_names)
    ):
        raise OdooSourceCaptureConsistencyError(
            "Odoo capture schema target or model scope changed"
        )
    stored_models = {item.name: item for item in stored.models}
    for model_name, model in live.models.items():
        stored_model = stored_models[model_name]
        if set(model.fields) != {item.name for item in stored_model.fields} or tuple(
            model.unique_constraints
        ) != tuple(stored_model.unique_constraints):
            raise OdooSourceCaptureConsistencyError("Odoo capture schema changed")
        stored_fields = {item.name: item for item in stored_model.fields}
        if any(
            not _same_field(stored_fields[name], field)
            for name, field in model.fields.items()
        ):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture field metadata changed"
            )


def _same_field(stored: SchemaField, live: FieldMetadata) -> bool:
    return (
        stored.name == live.name
        and stored.label == live.label
        and stored.type == live.type
        and stored.required == live.required
        and stored.readonly == live.readonly
        and stored.relation == live.relation
        and stored.relation_field == live.relation_field
        and stored.selection == live.selection
        and stored.stored == live.stored
        and stored.computed == live.computed
        and stored.has_inverse == live.has_inverse
        and stored.related == live.related
        and stored.translated == live.translated
        and stored.company_dependent == live.company_dependent
        and stored.searchable == live.searchable
        and stored.sortable == live.sortable
        and stored.exportable == live.exportable
        and stored.digits == live.digits
        and stored.currency_field == live.currency_field
    )
