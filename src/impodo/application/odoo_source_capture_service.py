"""Authorize and orchestrate one closed live Odoo-source page stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from impodo.domain.shared.access import Actor, Capability
from impodo.domain.odoo.contracts import MetadataSnapshot
from ..domain.odoo_capture import (
    OdooCaptureSelection,
    odoo_capture_selection_set_hash,
    require_consistent_odoo_capture_selection_set,
)
from ..domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ..domain.odoo_source_capture import (
    CancellationProbe,
    OdooCaptureAssessment,
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureSample,
    OdooSourceCaptureAccessRefreshRequired,
    OdooSourceCaptureConsistencyError,
    OdooSourceCaptureRequest,
    plan_odoo_source_capture,
    require_not_cancelled,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    OdooReadIdentity,
    ProtectedOdooReadContext,
)
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.workspace.access import WorkspaceAccessService


class OdooCaptureWorkspaceReader(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class OdooCaptureSelectionReader(Protocol):
    def get_current_odoo_capture_selections(
        self,
        workspace_id: str,
    ) -> tuple[OdooCaptureSelection, ...]: ...


class OdooCaptureSchemaReader(Protocol):
    def get_odoo_schema_catalog(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None: ...


class OdooSourceCaptureSession(Protocol):
    def pages(self): ...

    @property
    def matching_rows(self) -> int: ...

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

    def count_matching(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        limit: int,
        cancellation: CancellationProbe | None = None,
    ) -> int: ...

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
    matching_rows: int


@dataclass(frozen=True, slots=True)
class OdooCaptureSetAssessment:
    """One current count for every model plan in a frozen capture set."""

    selection_hash: str
    items: tuple[tuple[OdooCaptureSelection, OdooCaptureAssessment], ...]

    @property
    def matching_rows(self) -> int:
        return sum(assessment.matching_rows for _, assessment in self.items)

    @property
    def maximum_rows(self) -> int:
        return sum(assessment.maximum_rows for _, assessment in self.items)

    @property
    def exceeds_maximum(self) -> bool:
        return any(assessment.exceeds_maximum for _, assessment in self.items)

    @property
    def batch_count(self) -> int | None:
        if self.exceeds_maximum:
            return None
        return sum((assessment.batch_count or 0) for _, assessment in self.items)

    @property
    def page_size(self) -> int:
        """Return the largest request size for compact legacy summaries."""

        return max(assessment.page_size for _, assessment in self.items)


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
        observe_matching_rows: Callable[[int], None] | None = None,
        model: str | None = None,
    ) -> OdooSourceCaptureResult:
        """Validate at both ends and pass each bounded typed page to one sink."""

        contexts = (self._context(workspace_id, actor=actor, model=model),)
        results = self._capture_contexts(
            workspace_id,
            gateway,
            contexts,
            consume_page_factory=consume_page_factory,
            cancellation=cancellation,
            observe_matching_rows=(
                (
                    lambda _selection, matching_rows: observe_matching_rows(
                        matching_rows
                    )
                )
                if observe_matching_rows is not None
                else None
            ),
        )
        return results[0]

    def capture_all(
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
        observe_matching_rows: Callable[[OdooCaptureSelection, int], None]
        | None = None,
    ) -> tuple[OdooSourceCaptureResult, ...]:
        """Capture the complete model set under one pair of live checks."""

        contexts = self._contexts(
            workspace_id,
            actor=actor,
            require_complete=True,
        )
        return self._capture_contexts(
            workspace_id,
            gateway,
            contexts,
            consume_page_factory=consume_page_factory,
            cancellation=cancellation,
            observe_matching_rows=observe_matching_rows,
        )

    def _capture_contexts(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        contexts: tuple[
            tuple[
                OdooSourceCaptureRequest,
                OdooSchemaCatalog,
                OdooCaptureSelection,
            ],
            ...,
        ],
        *,
        consume_page_factory: Callable[
            [OdooSourceCaptureRequest, OdooCaptureSelection],
            Callable[[OdooCapturePage], None],
        ],
        cancellation: CancellationProbe | None,
        observe_matching_rows: Callable[[OdooCaptureSelection, int], None]
        | None,
    ) -> tuple[OdooSourceCaptureResult, ...]:
        """Stream one or more plans while sharing bounded verification work."""

        first_request, schema, _ = contexts[0]
        protected_context = self._verify_start(
            gateway,
            first_request,
            schema,
            cancellation=cancellation,
        )
        results: list[OdooSourceCaptureResult] = []
        for request, _, selection in contexts:
            require_not_cancelled(cancellation)
            consume_page = consume_page_factory(request, selection)
            session = gateway.open_capture(
                request,
                protected_context,
                cancellation=cancellation,
            )
            if observe_matching_rows is not None:
                observe_matching_rows(selection, session.matching_rows)
            for page in session.pages():
                require_not_cancelled(cancellation)
                consume_page(page)
            accounting = session.accounting
            results.append(
                OdooSourceCaptureResult(
                    request=request,
                    selection=selection,
                    accounting=accounting,
                    matching_rows=session.matching_rows,
                )
            )

        require_not_cancelled(cancellation)
        self._require_current_contexts(workspace_id, contexts)
        self._verify_end(
            gateway,
            first_request,
            schema,
            protected_context,
            cancellation=cancellation,
        )
        require_not_cancelled(cancellation)
        return tuple(results)

    def assess(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        actor: Actor,
        cancellation: CancellationProbe | None = None,
        model: str | None = None,
    ) -> OdooCaptureAssessment:
        """Count matching rows once without reading business-field values."""

        request, schema, _ = self._context(
            workspace_id,
            actor=actor,
            model=model,
        )
        protected_context = self._verify_start(
            gateway,
            request,
            schema,
            cancellation=cancellation,
        )
        matching_rows = gateway.count_matching(
            request,
            protected_context,
            limit=request.maximum_rows + 1,
            cancellation=cancellation,
        )
        require_not_cancelled(cancellation)
        return OdooCaptureAssessment(
            selection_hash=request.selection_hash,
            matching_rows=matching_rows,
            maximum_rows=request.maximum_rows,
            page_size=request.page_size,
            observed_at=datetime.now(timezone.utc),
        )

    def assess_all(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        actor: Actor,
        cancellation: CancellationProbe | None = None,
    ) -> OdooCaptureSetAssessment:
        """Count every current model plan without reading business values."""

        contexts = self._contexts(
            workspace_id,
            actor=actor,
            require_complete=True,
        )
        first_request, schema, _ = contexts[0]
        protected_context = self._verify_start(
            gateway,
            first_request,
            schema,
            cancellation=cancellation,
        )
        observed_at = datetime.now(timezone.utc)
        items = []
        for request, _, selection in contexts:
            require_not_cancelled(cancellation)
            matching_rows = gateway.count_matching(
                request,
                protected_context,
                limit=request.maximum_rows + 1,
                cancellation=cancellation,
            )
            items.append(
                (
                    selection,
                    OdooCaptureAssessment(
                        selection_hash=request.selection_hash,
                        matching_rows=matching_rows,
                        maximum_rows=request.maximum_rows,
                        page_size=request.page_size,
                        observed_at=observed_at,
                    ),
                )
            )
        selections = tuple(selection for _, _, selection in contexts)
        return OdooCaptureSetAssessment(
            selection_hash=odoo_capture_selection_set_hash(selections),
            items=tuple(items),
        )

    def sample(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        limit: int,
        actor: Actor,
        cancellation: CancellationProbe | None = None,
        model: str | None = None,
    ) -> OdooCaptureSample:
        """Return a bounded, explicitly non-authoritative sample."""

        request, schema, _ = self._context(
            workspace_id,
            actor=actor,
            model=model,
        )
        protected_context = self._verify_start(
            gateway,
            request,
            schema,
            cancellation=cancellation,
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

    @staticmethod
    def _verify_start(
        gateway: OdooSourceCapturePort,
        request: OdooSourceCaptureRequest,
        schema: OdooSchemaCatalog,
        *,
        cancellation: CancellationProbe | None,
    ) -> ProtectedOdooReadContext:
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
        return protected_context

    @staticmethod
    def _verify_end(
        gateway: OdooSourceCapturePort,
        request: OdooSourceCaptureRequest,
        schema: OdooSchemaCatalog,
        expected_context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None,
    ) -> None:
        end_identity, end_context = gateway.probe_identity(
            request,
            cancellation=cancellation,
        )
        _require_identity(request, end_identity)
        if end_context != expected_context:
            raise OdooSourceCaptureConsistencyError(
                "Odoo company access changed during capture"
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

    def _require_current_contexts(
        self,
        workspace_id: str,
        contexts: tuple[
            tuple[
                OdooSourceCaptureRequest,
                OdooSchemaCatalog,
                OdooCaptureSelection,
            ],
            ...,
        ],
    ) -> None:
        """Recheck current local pointers once after a streamed capture set."""

        current_by_model = {
            item.model: item for item in self._current_selection_set(workspace_id)
        }
        current_schema = self._schemas.get_odoo_schema_catalog(workspace_id)
        if current_schema is None:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture selection or schema changed during capture"
            )
        for request, _, selection in contexts:
            current = current_by_model.get(selection.model)
            if (
                current is None
                or current.content_hash != request.selection_hash
                or current_schema.content_hash
                != request.expected_schema_scope_hash
            ):
                raise OdooSourceCaptureConsistencyError(
                    "Odoo capture selection or schema changed during capture"
                )

    def _contexts(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        require_complete: bool,
    ) -> tuple[
        tuple[
            OdooSourceCaptureRequest,
            OdooSchemaCatalog,
            OdooCaptureSelection,
        ],
        ...,
    ]:
        """Build every local request once without contacting Odoo."""

        access = self._authorization.require(
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
        selections = self._current_selection_set(workspace_id)
        schema = self._schemas.get_odoo_schema_catalog(workspace_id)
        if not selections or schema is None:
            raise WorkspaceError(
                "Save a current Odoo capture selection and live schema first"
            )
        if require_complete and {item.model for item in selections} != {
            item.name for item in schema.models
        }:
            raise WorkspaceError(
                "Save a capture plan for every selected Odoo record type before "
                "checking or freezing records"
            )
        if schema.origin is not SchemaOrigin.LIVE_API:
            raise WorkspaceError(
                "Live Odoo source capture requires authenticated schema evidence"
            )
        contexts = []
        for selection in selections:
            if selection.data_version_id != access.data_version_id:
                raise WorkspaceError(
                    "The Odoo capture selection belongs to another DataVersion"
                )
            if selection.max_rows != CURRENT_ODOO_SOURCE_POLICY.max_rows:
                raise WorkspaceError(
                    "Review and save the Odoo capture plan before reading records"
                )
            contexts.append(
                (plan_odoo_source_capture(selection, schema), schema, selection)
            )
        require_consistent_odoo_capture_selection_set(selections)
        return tuple(contexts)

    def _context(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        model: str | None = None,
    ) -> tuple[
        OdooSourceCaptureRequest,
        OdooSchemaCatalog,
        OdooCaptureSelection,
    ]:
        contexts = self._contexts(
            workspace_id,
            actor=actor,
            require_complete=False,
        )
        if model is None:
            if len(contexts) != 1:
                raise WorkspaceError(
                    "Choose which Odoo record type to capture"
                )
            return contexts[0]
        else:
            selected = next(
                (
                    context
                    for context in contexts
                    if context[2].model == model
                ),
                None,
            )
            if selected is None:
                raise WorkspaceError(
                    "The requested Odoo record type has no current capture plan"
                )
            return selected

    def current_selections(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        require_complete: bool = False,
    ) -> tuple[OdooCaptureSelection, ...]:
        """Return the authorized, coherent current model-plan set."""

        self._authorization.require(
            actor,
            Capability.SOURCE_CAPTURE,
            workspace_id=workspace_id,
        )
        selections = self._current_selection_set(workspace_id)
        schema = self._schemas.get_odoo_schema_catalog(workspace_id)
        if not selections or schema is None:
            raise WorkspaceError(
                "Save a capture plan for each Odoo record type first"
            )
        if require_complete and {item.model for item in selections} != {
            item.name for item in schema.models
        }:
            raise WorkspaceError(
                "Save a capture plan for every selected Odoo record type before "
                "checking or freezing records"
            )
        return require_consistent_odoo_capture_selection_set(selections)

    def _current_selection_set(
        self,
        workspace_id: str,
    ) -> tuple[OdooCaptureSelection, ...]:
        return tuple(
            self._selections.get_current_odoo_capture_selections(workspace_id)
        )


def _require_identity(
    request: OdooSourceCaptureRequest,
    identity: OdooReadIdentity,
) -> None:
    if identity.target_hash != request.expected_connection_target_hash:
        raise OdooSourceCaptureAccessRefreshRequired(
            "The saved Odoo target no longer matches. Refresh Odoo details and "
            "review the change before reading records."
        )
    if identity.principal_hash != request.expected_read_principal_hash:
        raise OdooSourceCaptureAccessRefreshRequired(
            "The Odoo API user changed. Refresh Odoo details and review the "
            "change before reading records."
        )
    if identity.permission_hash != request.expected_read_permission_hash:
        raise OdooSourceCaptureAccessRefreshRequired(
            "The Odoo API user's read permissions changed. Refresh Odoo details "
            "and review the change before reading records."
        )
    if identity.context_hash != request.expected_context_hash:
        raise OdooSourceCaptureAccessRefreshRequired(
            "The saved Odoo access evidence uses an earlier verification format "
            "or the available company scope changed. Refresh Odoo details, then "
            "review and save the capture plan again. Your saved work is unchanged."
        )
    if identity.readable_models != request.schema_model_names:
        raise OdooSourceCaptureAccessRefreshRequired(
            "The Odoo model access scope changed. Refresh Odoo details and review "
            "the change before reading records."
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
