"""Expose the browser's governed Source data workflows.

Layer: web routing and presentation. This module owns the HTTP boundary for
both supported source modes:

* ``FILE`` projects inspect registered CSV/XLSX bytes, save per-file table
  choices, allow revision-checked replacement before the first freeze, and
  publish one immutable source selection.
* ``ODOO`` projects define a bounded record capture, keep the read credential
  in the secret store, bind that credential to freshly captured schema
  evidence, and monitor background publication of an immutable source
  snapshot.

Routes authenticate reads, validate CSRF-protected forms, translate expected
application failures into recoverable pages, and choose redirects. Business
rules and persistence remain in the intake, inspection, source-workspace,
credential, provenance, and capture-job services. The router never parses
source bytes, edits frozen evidence, or writes business records to Odoo.

Potentially expensive file intake, inspection, freezing, and capture work is
kept off the event loop or delegated to a background manager. Rendering may
read bounded workspace-level projections, but must not add an Odoo call or a
repository query per source row, field, or captured record.

See ``docs/developer/workflow/01-source-data.md``,
``docs/developer/contracts/evidence-lifecycle.md``, and
``tests/integration/web/test_source_workflow.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.browser_queries import SourceReviewPage
from impodo.application.data_version.inspection import (
    SourceInspectionError,
    SourceInspectionOptions,
)
from impodo.application.odoo_dependency_capture import MAX_DEPENDENCY_DEPTH
from impodo.application.odoo_read_failures import OdooReadCredentialMissingError
from impodo.application.shared.secrets import SecretStoreError
from impodo.application.workspace.odoo_capture_jobs import (
    OdooCaptureJob,
    OdooCaptureJobStatus,
)
from impodo.domain.guided_odoo_capture import (
    GuidedOdooCapturePlanDraft,
    propose_guided_odoo_capture_plans,
)
from impodo.domain.odoo.contracts import (
    ConnectorError,
    ConnectorIncompleteResultError,
    MetadataSnapshot,
    merge_disjoint_metadata_snapshots,
)
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.contracts import (
    OdooModelCatalog,
    OdooSchemaCatalog,
    SchemaModel,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStatus,
)

from ...application.odoo_capture_job_service import (
    OdooCaptureJobNotFoundError,
    OdooCaptureJobStateError,
)
from ...domain.data_version.models import DataVersionState
from ...domain.odoo_capture import (
    ODOO_CAPTURE_PAGE_SIZES,
    OdooCaptureRole,
    OdooCaptureSelection,
    odoo_capture_selection_set_hash,
)
from ...domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
    OdooRelationshipScopeError,
    propose_relationship_action_defaults,
    relationship_expansion_suggestions,
    relationship_expansion_suggestions_for_decisions,
    relationship_scope_decisions,
    review_relationship_scope,
)
from ...domain.odoo_source_capture import (
    OdooSourceCaptureAccessRefreshRequired,
    OdooSourceCaptureConfigurationError,
    OdooSourceCaptureError,
    is_odoo_capture_filter_field,
    is_odoo_capture_relationship_field,
    is_odoo_capture_value_field,
    plan_odoo_source_capture,
)
from ...domain.odoo_source_policy import (
    CURRENT_ODOO_SOURCE_POLICY,
    odoo_source_policy_from_hash,
)
from ...domain.odoo_source_scope import (
    RelatedDataHandling,
    RelatedDataSuggestion,
    propose_related_odoo_data,
    related_model_can_be_selected,
)
from ..composition.page_reads import run_local_operation, run_page_read
from ..composition.target_readers import (
    _missing_schema_reader_message,
    _selected_local_profile,
)
from ..context import SchemaReadSession, WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.navigation import build_odoo_capture_workspace_navigation
from ..presenters.odoo_source_scope import build_related_data_scope_view
from ..presenters.schema import (
    _dataset_choices,
    _dataset_choices_from,
    _decode_delimiter,
)
from ..security import require_session
from ..source_file_commands import accept_source_uploads, remove_source_file
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    get_target_credential_status,
    store_target_credential,
)
from .schema import _read_selected_schema_sync

_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY = "odoo_capture_assessment"
_LINKED_DATA_CHECK_IN_PROGRESS = (
    "Impodo is already checking linked data for this project. Wait for it to "
    "finish, then refresh."
)


class _WorkspaceOperationGate:
    """Reject overlapping process-local work for the same project."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_workspace_ids: set[str] = set()

    def try_start(self, workspace_id: str) -> bool:
        """Claim one project without waiting for its current operation."""

        with self._lock:
            if workspace_id in self._active_workspace_ids:
                return False
            self._active_workspace_ids.add(workspace_id)
            return True

    def finish(self, workspace_id: str) -> None:
        """Release a project after success or failure."""

        with self._lock:
            self._active_workspace_ids.discard(workspace_id)


async def _prepare_related_schema_candidate(
    context: WebContext,
    staged_state: WorkspaceState,
    model_catalog: OdooModelCatalog,
) -> OdooSchemaCatalog:
    """Read and validate one candidate relationship graph without publishing it."""

    def prepare():
        snapshot, binding_hash, read_identity, _local_profile = (
            _read_selected_schema_sync(context, staged_state)
        )
        return context.schema_workspace.prepare_capture(
            staged_state,
            snapshot,
            read_credential_binding_hash=binding_hash,
            read_identity=read_identity,
            discovered_model_catalog=model_catalog,
            actor=context.actor,
        )

    return await run_in_threadpool(prepare)


@dataclass(slots=True)
class _IncrementalRelatedSchemaSession:
    """Accumulate fresh schema deltas for one linked-data save only."""

    context: WebContext
    workspace_state: WorkspaceState
    model_catalog: OdooModelCatalog
    reader: SchemaReadSession
    credential_binding_hash: str
    snapshot: MetadataSnapshot | None = None

    @property
    def captured_models(self) -> frozenset[str]:
        """Return record types already read during this one request."""

        return (
            frozenset(self.snapshot.models)
            if self.snapshot is not None
            else frozenset()
        )

    def _snapshot_for(
        self,
        requested: frozenset[str],
    ) -> MetadataSnapshot:
        """Project fetched metadata onto one exact candidate model set."""

        if self.snapshot is None or not requested.issubset(self.snapshot.models):
            raise ConnectorIncompleteResultError(
                "Impodo could not complete the linked-data field check"
            )
        return replace(
            self.snapshot,
            models={
                model: self.snapshot.models[model]
                for model in sorted(requested)
            },
            create_defaults={
                model: self.snapshot.create_defaults[model]
                for model in sorted(requested)
                if model in self.snapshot.create_defaults
            },
            content_hash=None,
        )

    async def prepare_models(
        self,
        staged_state: WorkspaceState,
    ) -> tuple[SchemaModel, ...]:
        """Read only a candidate's new record types and project field shapes."""

        requested = frozenset(staged_state.intended_models)
        captured = self.captured_models
        new_models = tuple(sorted(requested - captured))
        if new_models:
            addition = await run_in_threadpool(
                self.reader.read_models,
                new_models,
            )
            if not addition.complete or set(addition.models) != set(new_models):
                raise ConnectorIncompleteResultError(
                    "Odoo did not return all field details needed for linked data"
                )
            if not set(addition.create_defaults).issubset(addition.models):
                raise ConnectorIncompleteResultError(
                    "Odoo returned field defaults for an unexpected record type"
                )
            addition = replace(addition, content_hash=None)
            self.snapshot = (
                addition
                if self.snapshot is None
                else merge_disjoint_metadata_snapshots(self.snapshot, addition)
            )
        candidate_snapshot = self._snapshot_for(requested)
        return await run_in_threadpool(
            self.context.schema_workspace.prepare_relationship_models,
            staged_state,
            candidate_snapshot,
            discovered_model_catalog=self.model_catalog,
            actor=self.context.actor,
        )

    async def finalize(
        self,
        staged_state: WorkspaceState,
    ) -> OdooSchemaCatalog:
        """Verify final access and build publishable evidence exactly once."""

        final_models = tuple(sorted(staged_state.intended_models))
        final_snapshot = self._snapshot_for(frozenset(final_models))
        read_identity = await run_in_threadpool(
            self.reader.verify_identity,
            final_models,
        )

        def prepare_final() -> OdooSchemaCatalog:
            current_credential = get_target_credential(
                self.context.secret_store,
                self.workspace_state,
                TargetCredentialRole.READ,
            )
            if (
                current_credential is None
                or current_credential.binding_hash
                != self.credential_binding_hash
            ):
                raise WorkspaceStateError(
                    "Odoo read access changed while Impodo was checking linked "
                    "data. Reload and try again."
                )
            return self.context.schema_workspace.prepare_capture(
                staged_state,
                final_snapshot,
                read_credential_binding_hash=self.credential_binding_hash,
                read_identity=read_identity,
                discovered_model_catalog=self.model_catalog,
                actor=self.context.actor,
            )

        return await run_in_threadpool(prepare_final)


def _open_incremental_related_schema_session(
    context: WebContext,
    workspace_state: WorkspaceState,
    model_catalog: OdooModelCatalog,
) -> _IncrementalRelatedSchemaSession | None:
    """Open a remote request-local session; keep local shell reads unchanged."""

    local_profile = _selected_local_profile(context, workspace_state)
    credential = get_target_credential(
        context.secret_store,
        workspace_state,
        TargetCredentialRole.READ,
    )
    if local_profile is not None and credential is None:
        return None
    if credential is None:
        raise OdooReadCredentialMissingError(
            _missing_schema_reader_message(workspace_state)
        )
    if context.schema_read_session_factory is None:
        return None
    return _IncrementalRelatedSchemaSession(
        context=context,
        workspace_state=workspace_state,
        model_catalog=model_catalog,
        reader=context.schema_read_session_factory(
            workspace_state,
            credential.secret,
        ),
        credential_binding_hash=credential.binding_hash,
    )


@contextmanager
def _diagnostic_related_data_stage(
    request: Request,
    stage: str,
    *,
    pass_number: int | None = None,
    total_record_types: int | None = None,
    new_record_types: int | None = None,
):
    """Time one privacy-safe linked-data discovery stage."""

    recorder = getattr(request.app.state, "diagnostic_recorder", None)
    started = perf_counter()
    try:
        yield
    except Exception as error:
        if recorder is not None:
            recorder.record_operation_stage(
                "odoo_related_data",
                stage,
                duration_ms=(perf_counter() - started) * 1000,
                outcome="failed",
                exception_class=type(error).__name__,
                pass_number=pass_number,
                total_record_types=total_record_types,
                new_record_types=new_record_types,
            )
        raise
    else:
        if recorder is not None:
            recorder.record_operation_stage(
                "odoo_related_data",
                stage,
                duration_ms=(perf_counter() - started) * 1000,
                pass_number=pass_number,
                total_record_types=total_record_types,
                new_record_types=new_record_types,
            )


@contextmanager
def _diagnostic_assessment_stage(request: Request, stage: str):
    """Time one assessment stage without recording source identifiers."""

    recorder = getattr(request.app.state, "diagnostic_recorder", None)
    started = perf_counter()
    try:
        yield
    except Exception as error:
        if recorder is not None:
            recorder.record_operation_stage(
                "odoo_capture_assessment",
                stage,
                duration_ms=(perf_counter() - started) * 1000,
                outcome="failed",
                exception_class=type(error).__name__,
            )
        raise
    else:
        if recorder is not None:
            recorder.record_operation_stage(
                "odoo_capture_assessment",
                stage,
                duration_ms=(perf_counter() - started) * 1000,
            )


def _publish_guided_capture_plans(
    context: WebContext,
    workspace_state: WorkspaceState,
    model_catalog: OdooModelCatalog,
    schema: OdooSchemaCatalog,
    relationship_scope: OdooRelationshipScope,
    drafts: tuple[GuidedOdooCapturePlanDraft, ...],
    *,
    current_selections: tuple[OdooCaptureSelection, ...] = (),
) -> tuple[WorkspaceState, tuple[OdooCaptureSelection, ...]]:
    """Atomically publish reviewed manual plans plus safe guided plans."""

    capture_selections = context.sources.build_guided_odoo_capture_selections(
        workspace_state.workspace_id,
        drafts=drafts,
        current_selections=current_selections,
        schema=schema,
        relationship_scope=relationship_scope,
        actor=context.actor,
    )
    saved = context.workspace_states.update_schema_scope(
        workspace_state.workspace_id,
        actor=context.actor,
        expected_revision=workspace_state.revision,
        permitted_models=tuple(
            sorted(relationship_scope.required_capture_models)
        ),
        relationship_decisions=relationship_scope.decisions,
        relationship_root_models=relationship_scope.root_models,
        relationship_explicit_actions=(
            relationship_scope.explicit_actions_by_model
        ),
        schema_catalog=schema,
        capture_selections=capture_selections,
        expected_model_catalog_content_hash=model_catalog.content_hash,
    )
    return saved, capture_selections


def build_sources_router(context: WebContext) -> APIRouter:
    """Build file-source and Odoo-source routes from one application context.

    The returned router is intentionally thin: it translates HTTP input and
    output while ``WebContext`` services enforce revisions, evidence bindings,
    source-mode restrictions, bounded capture policy, and publication rules.
    """

    router = APIRouter()
    related_data_checks = _WorkspaceOperationGate()

    @router.get("/workspaces/{workspace_id}/sources", response_class=HTMLResponse)
    async def workspace_sources(request: Request, workspace_id: str):
        """Render the current source-mode page or its active capture job.

        Draft projects return to setup. Registered Odoo-source projects resume
        the one active background capture when present; file projects render
        catalogues and configurations and expose file removal only before a
        frozen source selection exists.
        """

        require_session(request)
        return await run_page_read(render_sources, request, workspace_id)

    def render_sources(request: Request, workspace_id: str):
        """Read source choices and render within one database-scoped worker."""

        source_page = context.queries.get_source_page(workspace_id)
        workspace_state = source_page.workspace_state
        if workspace_state.status is not WorkspaceStatus.REGISTERED:
            setup_page = (
                "files" if workspace_state.source_mode is SourceMode.FILE else "target"
            )
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/{setup_page}",
                status_code=303,
            )
        if workspace_state.source_mode is SourceMode.ODOO:
            active = (
                context.odoo_capture_jobs.active(workspace_id)
                if context.odoo_capture_jobs is not None
                else None
            )
            if active is not None:
                return RedirectResponse(
                    _odoo_capture_progress_url(workspace_id, active.job_id),
                    status_code=303,
                )
            return _render_odoo_capture_selection(request, context, workspace_state)
        pending_error = request.session.get("source_inspection_error")
        inspection_error = None
        if (
            isinstance(pending_error, dict)
            and pending_error.get("workspace_id") == workspace_id
        ):
            request.session.pop("source_inspection_error", None)
            message = pending_error.get("message")
            if message:
                inspection_error = str(message)
        return _render_file_sources(
            request,
            context,
            workspace_id,
            source_page=source_page,
            error=inspection_error,
            status_code=422 if inspection_error else 200,
        )

    @router.post("/workspaces/{workspace_id}/files/{file_id}/remove")
    async def remove_workspace_source_file(
        request: Request,
        workspace_id: str,
        file_id: str,
    ):
        """Remove one contained file and its checks before dataset freezing.

        The intake service owns revision checking, path containment, audit,
        catalogue cleanup, and the fail-closed frozen-selection boundary. This
        route only validates the permitted return page and renders fresh state
        when the command is rejected.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "return_to"})
        return_to = _text(form, "return_to")
        if return_to not in {"files", "sources", "datasets"}:
            raise HTTPException(status_code=400, detail="Invalid return page")
        try:
            removed = await remove_source_file(
                context,
                workspace_id,
                file_id,
                expected_revision=_revision(form),
            )
        except WorkspaceStateError as error:
            return _render_source_file_error(
                request,
                context,
                workspace_id,
                return_to,
                error,
            )
        _flash(request, f"Removed {removed.display_name} from this Data version.")
        suffix = "#source-files" if return_to == "sources" else ""
        return RedirectResponse(
            f"/workspaces/{workspace_id}/{return_to}{suffix}",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/files")
    async def add_registered_workspace_source_file(
        request: Request,
        workspace_id: str,
    ):
        """Add one bounded CSV/XLSX file before dataset freezing.

        Upload streaming, size/type validation, hashing, contained storage, and
        audit belong to the intake service and run outside the event loop. The
        uploaded handle is closed on every success or failure path.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "source_file"})
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.status is not WorkspaceStatus.REGISTERED
            or workspace_state.source_mode is not SourceMode.FILE
        ):
            raise HTTPException(status_code=400, detail="Source upload is unavailable")
        try:
            added_files = await accept_source_uploads(
                context,
                workspace_id,
                form,
                allow_multiple=False,
            )
        except WorkspaceStateError as error:
            return _render_source_file_error(
                request,
                context,
                workspace_id,
                "sources",
                error,
            )
        _flash(
            request,
            f"Added {added_files[0].display_name} to this Data version.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/sources#source-files",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/odoo-selection")
    async def save_odoo_capture_selection(request: Request, workspace_id: str):
        """Save a bounded protected capture plan without reading Odoo rows.

        The source-workspace service validates the chosen model, eligible
        fields, archive policy, row ceiling, and source-mode prerequisites
        against current schema evidence. Saving the plan creates no snapshot
        and grants no write capability.
        """

        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "dataset_name",
                "model",
                "field_names",
                "include_archived",
                "page_size",
                "filter_field",
                "filter_value",
                "remove_filter",
                "linked_only",
            },
        )

        def save_selection():
            selection = context.sources.define_odoo_capture_selection(
                workspace_id,
                dataset_name=_text(form, "dataset_name"),
                model=_text(form, "model"),
                field_names=tuple(form.getlist("field_names")),
                include_archived=bool(_text(form, "include_archived")),
                page_size=_text(form, "page_size"),
                filter_field=_text(form, "filter_field"),
                filter_value=_text(form, "filter_value"),
                remove_filter=bool(_text(form, "remove_filter")),
                linked_only=bool(_text(form, "linked_only")),
                actor=context.actor,
            )
            request.session.pop(_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY, None)
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            current_selections = (
                context.queries.get_current_odoo_capture_selections(workspace_id)
                if schema is not None
                else ()
            )
            relationship_scope = (
                context.queries.get_current_odoo_relationship_scope(
                    workspace_id
                )
            )
            if schema is not None and relationship_scope is not None:
                proposal = propose_guided_odoo_capture_plans(
                    schema,
                    relationship_scope,
                    current_selections=current_selections,
                )
                if proposal.issues:
                    next_issue = next(
                        (
                            issue
                            for issue in proposal.issues
                            if issue.model is not None
                        ),
                        proposal.issues[0],
                    )
                    return selection, next_issue, (), 0
                model_catalog = context.queries.get_odoo_model_catalog(
                    workspace_id
                )
                if model_catalog is None:
                    raise WorkspaceError(
                        "Refresh the available Odoo record types before "
                        "preparing capture plans"
                    )
                workspace_state = context.queries.get(workspace_id)
                _saved, complete_selections = _publish_guided_capture_plans(
                    context,
                    workspace_state,
                    model_catalog,
                    schema,
                    relationship_scope,
                    proposal.drafts,
                    current_selections=current_selections,
                )
                return (
                    selection,
                    None,
                    (),
                    len(complete_selections) - len(current_selections),
                )
            planned_models = {item.model for item in current_selections}
            missing_models = tuple(
                item
                for item in (schema.models if schema is not None else ())
                if item.name not in planned_models
            )
            return selection, None, missing_models, 0

        try:
            selection, next_issue, missing_models, generated_count = (
                await run_local_operation(save_selection)
            )
        except (WorkspaceError, WorkspaceStateError) as error:
            def render_error():
                return _render_odoo_capture_selection(
                    request,
                    context,
                    context.queries.get(workspace_id),
                    error=str(error),
                    status_code=422,
                )

            return await run_page_read(render_error)
        if next_issue is not None:
            _flash(
                request,
                f"Saved {selection.model}. Review the remaining exception for "
                f"{next_issue.model_label}.",
            )
            suffix = (
                f"?model={next_issue.model}&edit=1"
                if next_issue.model is not None
                else ""
            )
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources{suffix}#capture-plan",
                status_code=303,
            )
        if missing_models:
            next_model = missing_models[0]
            _flash(
                request,
                f"Saved {selection.model}. Next, save a plan for "
                f"{next_model.label}.",
            )
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources?model={next_model.name}"
                "#capture-plan",
                status_code=303,
            )
        _flash(
            request,
            (
                f"Saved {selection.model} and prepared {generated_count} safe "
                "linked-data capture plans automatically. Count the selected "
                "source records to continue."
                if generated_count
                else "All Odoo capture plans are saved. Count the selected "
                "source records to continue."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/sources#capture-next-action",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/odoo-related-data")
    async def save_odoo_related_data(request: Request, workspace_id: str):
        """Reject duplicate saves before staging one linked-data plan."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "related_actions"},
        )
        if not related_data_checks.try_start(workspace_id):
            return HTMLResponse(
                _LINKED_DATA_CHECK_IN_PROGRESS,
                status_code=409,
            )
        try:
            return await _save_odoo_related_data(
                request,
                workspace_id,
                form,
            )
        finally:
            related_data_checks.finish(workspace_id)

    async def _save_odoo_related_data(request, workspace_id, form):
        """Stage the complete linked-data plan, then publish it once."""

        def submitted_actions() -> dict[
            str, OdooRelationshipCaptureAction
        ]:
            submitted: dict[str, OdooRelationshipCaptureAction] = {}
            for raw in form.getlist("related_actions"):
                model, separator, raw_action = str(raw).partition("::")
                try:
                    action = OdooRelationshipCaptureAction(raw_action)
                except ValueError as error:
                    raise WorkspaceStateError(
                        "Choose a current action for each related record type"
                    ) from error
                if not separator or not model or model in submitted:
                    raise WorkspaceStateError(
                        "Choose one action for each related record type"
                    )
                submitted[model] = action
            return submitted

        def relationship_root_models(
            workspace_state: WorkspaceState,
            current_scope: OdooRelationshipScope | None,
        ) -> set[str]:
            if current_scope is None:
                return set(workspace_state.intended_models)
            if current_scope.contract_version >= 2:
                return set(current_scope.root_models)
            # Contract v1 inferred roots from the prior graph. Persist that
            # best available evidence when it is upgraded.
            return (
                set(workspace_state.intended_models)
                - current_scope.included_models
            )

        def deliberate_overrides(
            submitted: dict[str, OdooRelationshipCaptureAction],
            *,
            suggestions: tuple[RelatedDataSuggestion, ...],
            root_models: set[str],
            current_scope: OdooRelationshipScope | None,
            carried: dict[str, OdooRelationshipCaptureAction],
        ) -> dict[str, OdooRelationshipCaptureAction]:
            """Retain deliberate exceptions without promoting HTML defaults."""

            recommended = propose_relationship_action_defaults(
                suggestions,
                root_models=root_models,
            )
            effective = propose_relationship_action_defaults(
                suggestions,
                root_models=root_models,
                current_scope=current_scope,
            )
            exception_models = (
                set(recommended.conflicting_models)
                | set(effective.conflicting_models)
            )
            desired = dict(carried)
            selectable = {
                item.relation_model
                for item in suggestions
                if related_model_can_be_selected(item.handling)
            }
            for model, action in submitted.items():
                if model not in selectable:
                    continue
                if model in exception_models:
                    desired[model] = action
                elif action is not effective.actions.get(model):
                    if action is recommended.actions.get(model):
                        desired.pop(model, None)
                    else:
                        desired[model] = action
            return desired

        def initial_state():
            workspace_state = context.queries.get(workspace_id)
            if (
                workspace_state.status is not WorkspaceStatus.REGISTERED
                or workspace_state.source_mode is not SourceMode.ODOO
            ):
                raise WorkspaceStateError(
                    "Related Odoo data can be changed only for a registered Odoo source"
                )
            if workspace_state.revision != expected_revision:
                raise WorkspaceStateError(
                    "The workspace changed in another request; reload before continuing"
                )
            model_catalog = context.queries.get_odoo_model_catalog(workspace_id)
            if model_catalog is None:
                raise WorkspaceStateError(
                    "Refresh the available Odoo record types before rebuilding linked data"
                )
            current_scope = context.queries.get_current_odoo_relationship_scope(
                workspace_id
            )
            current_schema = context.queries.get_odoo_schema_catalog(workspace_id)
            roots = relationship_root_models(workspace_state, current_scope)
            if not roots:
                raise WorkspaceStateError(
                    "Choose at least one main Odoo record type before linked data"
                )
            available = {item.name for item in model_catalog.models}
            missing_root = sorted(roots - available)
            if missing_root:
                raise WorkspaceStateError(
                    f"{missing_root[0]} is no longer available to the Odoo read user"
                )
            return (
                workspace_state,
                current_scope,
                current_schema,
                model_catalog,
                roots,
                available,
            )

        def decisions_for_models(
            schema_models: tuple[SchemaModel, ...],
            *,
            current_scope: OdooRelationshipScope | None,
            roots: set[str],
            available: set[str],
            explicit_actions: dict[str, OdooRelationshipCaptureAction],
            frontier_decisions: tuple[OdooRelationshipScopeDecision, ...],
        ):
            all_suggestions = propose_related_odoo_data(
                schema_models,
                include_selected=True,
            )
            frontier_suggestions = (
                relationship_expansion_suggestions_for_decisions(
                    all_suggestions,
                    frontier_decisions,
                    root_models=roots,
                )
            )
            selectable = {
                item.relation_model
                for item in frontier_suggestions
                if related_model_can_be_selected(item.handling)
            }
            applicable_explicit = {
                model: action
                for model, action in explicit_actions.items()
                if model in selectable
            }
            defaults = propose_relationship_action_defaults(
                frontier_suggestions,
                root_models=roots,
                current_scope=current_scope,
                explicit_actions_by_model=applicable_explicit,
            )
            preliminary = relationship_scope_decisions(
                frontier_suggestions,
                actions_by_model={
                    model: action
                    for model, action in defaults.actions.items()
                    if model in available
                },
                available_models=available,
                unresolved_models=defaults.conflicting_models,
            )
            final_suggestions = relationship_expansion_suggestions_for_decisions(
                all_suggestions,
                preliminary,
                root_models=roots,
            )
            final_selectable = {
                item.relation_model
                for item in final_suggestions
                if related_model_can_be_selected(item.handling)
                and item.relation_model in available
            }
            final_explicit = {
                model: action
                for model, action in explicit_actions.items()
                if model in final_selectable
            }
            final_defaults = propose_relationship_action_defaults(
                final_suggestions,
                root_models=roots,
                current_scope=current_scope,
                explicit_actions_by_model=final_explicit,
            )
            decisions = relationship_scope_decisions(
                final_suggestions,
                actions_by_model={
                    model: action
                    for model, action in final_defaults.actions.items()
                    if model in available
                },
                available_models=available,
                unresolved_models=final_defaults.conflicting_models,
            )
            included = {
                item.relation_model
                for item in decisions
                if item.action in {
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                    OdooRelationshipCaptureAction.PRESERVE_LINKED,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                }
            }
            return (
                decisions,
                roots | included,
                explicit_actions,
                final_selectable,
                final_defaults.conflicting_models,
            )

        try:
            submitted = submitted_actions()
            expected_revision = _revision(form)
            (
                workspace_state,
                current_scope,
                current_schema,
                model_catalog,
                root_models,
                available_models,
            ) = await run_local_operation(initial_state)
            explicit_actions = (
                current_scope.explicit_actions
                if current_scope is not None
                and current_scope.contract_version >= 6
                else {}
            )
            if current_schema is not None:
                # Classify browser submissions exactly once against the
                # persisted graph that rendered the form. Dependency expansion
                # can expose a new conflict, but it must not turn an untouched
                # HTML default into deliberate user intent merely because that
                # conflict was not visible when the user submitted the page.
                rendered_suggestions = relationship_expansion_suggestions(
                    propose_related_odoo_data(
                        current_schema.models,
                        include_selected=True,
                    ),
                    current_scope,
                )
                explicit_actions = deliberate_overrides(
                    submitted,
                    suggestions=rendered_suggestions,
                    root_models=root_models,
                    current_scope=current_scope,
                    carried=explicit_actions,
                )
            elif submitted:
                # A schema-less recovery page never renders relationship
                # choices. Do not accept choices that the data manager could
                # not have reviewed, but allow its empty rebuild submission.
                raise WorkspaceStateError(
                    "Rebuild the Odoo fields before choosing linked-data actions"
                )
            candidate_models = set(root_models)
            prepared_schema = None
            final_decisions = ()
            frontier_decisions = (
                current_scope.decisions if current_scope is not None else ()
            )
            final_conflicting_models: tuple[str, ...] = ()
            incremental_session = await run_in_threadpool(
                _open_incremental_related_schema_session,
                context,
                workspace_state,
                model_catalog,
            )
            for pass_index in range(MAX_DEPENDENCY_DEPTH + 1):
                staged_state = replace(
                    workspace_state,
                    intended_models=tuple(sorted(candidate_models)),
                )
                new_record_types = (
                    len(
                        candidate_models
                        - set(incremental_session.captured_models)
                    )
                    if incremental_session is not None
                    else None
                )
                with _diagnostic_related_data_stage(
                    request,
                    "schema_capture_pass",
                    pass_number=pass_index + 1,
                    total_record_types=len(candidate_models),
                    new_record_types=new_record_types,
                ):
                    if incremental_session is None:
                        prepared_schema = (
                            await _prepare_related_schema_candidate(
                                context,
                                staged_state,
                                model_catalog,
                            )
                        )
                        staged_models = prepared_schema.models
                    else:
                        staged_models = (
                            await incremental_session.prepare_models(
                                staged_state
                            )
                        )
                (
                    final_decisions,
                    chosen_models,
                    explicit_actions,
                    final_selectable,
                    final_conflicting_models,
                ) = (
                    decisions_for_models(
                        staged_models,
                        current_scope=current_scope,
                        roots=root_models,
                        available=available_models,
                        explicit_actions=explicit_actions,
                        frontier_decisions=frontier_decisions,
                    )
                )
                frontier_decisions = final_decisions
                if chosen_models == candidate_models:
                    explicit_actions = {
                        model: action
                        for model, action in explicit_actions.items()
                        if model in final_selectable
                    }
                    break
                if pass_index >= MAX_DEPENDENCY_DEPTH:
                    raise WorkspaceStateError(
                        "The recommended related-data plan reaches beyond "
                        f"the {MAX_DEPENDENCY_DEPTH}-step relationship limit. "
                        "Review that graph under Advanced."
                    )
                candidate_models = chosen_models
            if incremental_session is not None:
                final_state = replace(
                    workspace_state,
                    intended_models=tuple(sorted(candidate_models)),
                )
                with _diagnostic_related_data_stage(
                    request,
                    "final_access_check",
                    total_record_types=len(candidate_models),
                    new_record_types=0,
                ):
                    prepared_schema = await incremental_session.finalize(
                        final_state
                    )
            assert prepared_schema is not None
            staged_scope = OdooRelationshipScope.create(
                scope_id=(current_scope.scope_id if current_scope else str(uuid4())),
                version=(current_scope.version + 1 if current_scope else 1),
                decisions=tuple(final_decisions),
                root_models=tuple(sorted(root_models)),
                explicit_actions_by_model=tuple(sorted(explicit_actions.items())),
                recorded_at=datetime.now(timezone.utc),
                recorded_by=context.actor.identity.display_name,
            )
            final_suggestions = relationship_expansion_suggestions_for_decisions(
                propose_related_odoo_data(
                    prepared_schema.models,
                    include_selected=True,
                ),
                final_decisions,
                root_models=root_models,
            )
            relationship_review = review_relationship_scope(
                final_suggestions,
                staged_scope,
                available_models=available_models,
            )
            guided_proposal = (
                propose_guided_odoo_capture_plans(
                    prepared_schema,
                    staged_scope,
                )
                if relationship_review.complete
                else None
            )
            generated_plan_count = 0
            if guided_proposal is None or guided_proposal.issues:
                relationship_scope_unchanged = bool(
                    current_scope is not None
                    and current_scope.contract_version >= 6
                    and current_scope.decisions == tuple(final_decisions)
                    and current_scope.root_models
                    == tuple(sorted(root_models))
                    and current_scope.explicit_actions_by_model
                    == tuple(sorted(explicit_actions.items()))
                )
                schema_unchanged = bool(
                    current_schema is not None
                    and current_schema.content_hash
                    == prepared_schema.content_hash
                    and current_schema.connection_target_hash
                    == prepared_schema.connection_target_hash
                    and current_schema.read_principal_hash
                    == prepared_schema.read_principal_hash
                    and current_schema.read_permission_hash
                    == prepared_schema.read_permission_hash
                    and current_schema.read_context_hash
                    == prepared_schema.read_context_hash
                )
                if not (
                    workspace_state.intended_models
                    == tuple(sorted(candidate_models))
                    and relationship_scope_unchanged
                    and schema_unchanged
                ):
                    await run_local_operation(
                        context.workspace_states.update_schema_scope,
                        workspace_id,
                        actor=context.actor,
                        expected_revision=expected_revision,
                        permitted_models=tuple(sorted(candidate_models)),
                        relationship_decisions=tuple(final_decisions),
                        relationship_root_models=tuple(sorted(root_models)),
                        relationship_explicit_actions=tuple(
                            sorted(explicit_actions.items())
                        ),
                        schema_catalog=prepared_schema,
                        expected_model_catalog_content_hash=(
                            model_catalog.content_hash
                        ),
                    )
            else:
                def publish_complete_capture_plan():
                    return _publish_guided_capture_plans(
                        context,
                        workspace_state,
                        model_catalog,
                        prepared_schema,
                        staged_scope,
                        guided_proposal.drafts,
                    )

                _saved_workspace_state, capture_selections = (
                    await run_local_operation(publish_complete_capture_plan)
                )
                generated_plan_count = len(capture_selections)
        except (
            ConnectorError,
            WorkspaceStateError,
            OdooRelationshipScopeError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            def render_error():
                current = context.queries.get(workspace_id)
                return _render_odoo_capture_selection(
                    request,
                    context,
                    current,
                    error=str(error),
                    status_code=422,
                )

            return await run_page_read(render_error)
        request.session.pop(_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY, None)
        if not relationship_review.complete:
            if relationship_review.unavailable:
                count = len(
                    {
                        item.relation_model
                        for item in relationship_review.unavailable
                    }
                )
                _flash(
                    request,
                    f"{count} related record type"
                    f"{'s are' if count != 1 else ' is'} unavailable. Refresh "
                    "the available Odoo record types before continuing.",
                )
            elif final_conflicting_models:
                count = len(final_conflicting_models)
                _flash(
                    request,
                    f"Impodo found {count} relationship exception"
                    f"{'s' if count != 1 else ''} needing a decision.",
                )
            else:
                count = len(relationship_review.pending)
                _flash(
                    request,
                    f"Impodo found {count} relationship"
                    f"{'s' if count != 1 else ''} needing review.",
                )
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources#related-data-scope",
                status_code=303,
            )
        assert guided_proposal is not None
        if guided_proposal.issues:
            count = len(guided_proposal.issues)
            _flash(
                request,
                f"Related-data review complete. Review {count} named capture-plan "
                f"exception{'s' if count != 1 else ''}.",
            )
            first_model = next(
                (
                    issue.model
                    for issue in guided_proposal.issues
                    if issue.model is not None
                ),
                None,
            )
            return RedirectResponse(
                (
                    f"/workspaces/{workspace_id}/sources?model={first_model}"
                    "&edit=1#capture-plan"
                    if first_model is not None
                    else f"/workspaces/{workspace_id}/sources#capture-plan"
                ),
                status_code=303,
            )
        if generated_plan_count:
            message = (
                f"Related-data review complete. Impodo prepared "
                f"{generated_plan_count} capture plan"
                f"{'s' if generated_plan_count != 1 else ''} automatically. "
                "Check the summary, then continue."
            )
        else:
            message = (
                "Related-data review complete. The current capture plans "
                "are already ready. Check the summary, then continue."
            )
        _flash(request, message)
        return RedirectResponse(
            f"/workspaces/{workspace_id}/sources#capture-next-action",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/odoo-assessment")
    async def assess_odoo_capture(request: Request, workspace_id: str):
        """Count matching records before asking for capture confirmation."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "selection_id", "selection_hash", "confirm_linked_relationships"},
        )
        try:
            def prepare_assessment():
                workspace_state = context.queries.get(workspace_id)
                workspace = context.migration_workspaces.get(
                    workspace_id,
                    actor=context.actor,
                )
                data_version = context.data_versions.get(
                    workspace.data_version_id,
                    actor=context.actor,
                )
                if data_version.state is not DataVersionState.DRAFT:
                    return workspace_state, workspace.project_id, None, None
                prepared = context.odoo_source_capture.prepare_assessment(
                    workspace_id,
                    actor=context.actor,
                )
                selections = prepared.selections
                schema = prepared.schema
                if (
                    any(
                        item.capture_role is OdooCaptureRole.LINKED_ONLY
                        for item in selections
                    )
                    and not _text(form, "confirm_linked_relationships")
                ):
                    raise WorkspaceError(
                        "Review and confirm the selected relationship fields "
                        "before checking linked records"
                    )
                selection_set_hash = odoo_capture_selection_set_hash(selections)
                submitted_hash = _text(form, "selection_hash")
                legacy_selection = selections[0] if len(selections) == 1 else None
                if submitted_hash not in {
                    selection_set_hash,
                    legacy_selection.content_hash if legacy_selection else "",
                }:
                    raise WorkspaceError(
                        "This page is out of date. Reload and check the current "
                        "Odoo capture plans."
                    )
                if any(
                    not _selection_uses_current_capture_policy(selection)
                    for selection in selections
                ):
                    raise WorkspaceError(
                        "Review and save these capture plans before checking "
                        "matching records."
                    )
                credential = get_target_credential(
                    context.secret_store,
                    workspace_state,
                    TargetCredentialRole.READ,
                )
                if credential is None:
                    raise WorkspaceError(
                        "Save a read-only Odoo API key before checking matching "
                        "records."
                    )
                if schema.read_credential_binding_hash != credential.binding_hash:
                    raise WorkspaceError(
                        "The Odoo read credential changed. Refresh the record "
                        "types and fields first."
                    )
                if schema.pending_refresh is not None:
                    raise WorkspaceError(
                        "Odoo fields changed. Review the checked Odoo changes first."
                    )
                return workspace_state, None, credential, prepared

            with _diagnostic_assessment_stage(request, "local_prepare"):
                (
                    workspace_state,
                    redirect_project_id,
                    credential,
                    prepared,
                ) = await run_local_operation(prepare_assessment)
            if redirect_project_id is not None:
                return RedirectResponse(
                    f"/projects/{redirect_project_id}/odoo-source-restarts/"
                    f"{workspace_id}?resume=source",
                    status_code=307,
                )
            assert credential is not None
            assert prepared is not None
            gateway = context.source_capture_factory(
                workspace_state,
                credential.secret,
            )
            with _diagnostic_assessment_stage(request, "remote_assessment"):
                assessment = await run_in_threadpool(
                    context.odoo_source_capture.assess_prepared,
                    prepared,
                    gateway,
                )

            def finalize_assessment():
                context.odoo_source_capture.require_current_assessment(prepared)
                return _render_odoo_capture_selection(
                    request,
                    context,
                    workspace_state,
                    assessment=assessment,
                )

            with _diagnostic_assessment_stage(request, "local_finalize"):
                response = await run_page_read(finalize_assessment)
            request.session[_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY] = {
                "workspace_id": workspace_id,
                "selection_hash": assessment.selection_hash,
                "matching_rows": assessment.matching_rows,
                "items": [
                    {
                        "model": selection.model,
                        "selection_hash": selection.content_hash,
                        "matching_rows": item.matching_rows,
                        "page_size": item.page_size,
                    }
                    for selection, item in assessment.items
                ],
            }
            return response
        except (
            ConnectorError,
            OdooSourceCaptureError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            def render_error():
                return _render_odoo_capture_selection(
                    request,
                    context,
                    context.queries.get(workspace_id),
                    error=str(error),
                    access_refresh_required=isinstance(
                        error,
                        OdooSourceCaptureAccessRefreshRequired,
                    ),
                    status_code=422,
                )

            with _diagnostic_assessment_stage(request, "local_error_render"):
                return await run_page_read(render_error)

    @router.post("/workspaces/{workspace_id}/sources/odoo-read-credential")
    async def save_odoo_capture_credential(request: Request, workspace_id: str):
        """Store and audit the read credential required by live capture.

        Secret bytes remain in the configured secret store. Only credential
        binding metadata is audited with the project. The redirect to schema
        is deliberate: model and field evidence must be refreshed under the
        new credential before a capture may start.
        """

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "read_api_key", "remember_read_api_key"},
        )
        workspace_state = context.queries.get(workspace_id)
        if workspace_state.source_mode is not SourceMode.ODOO:
            raise HTTPException(status_code=404, detail="Odoo source not found")
        try:
            credential = store_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.READ,
                _text(form, "read_api_key"),
                persistent=bool(_text(form, "remember_read_api_key")),
            )
            audit_stored_target_credential(
                context.workspace_states,
                workspace_state,
                TargetCredentialRole.READ,
                credential,
                actor=context.actor,
            )
        except (WorkspaceStateError, SecretStoreError, WorkspaceError) as error:
            return _render_odoo_capture_selection(
                request,
                context,
                workspace_state,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Saved the read-only Odoo key. Refresh the record types and fields so the capture is bound to this credential.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/odoo-capture")
    async def start_odoo_capture(request: Request, workspace_id: str):
        """Confirm the exact current plan set and enqueue snapshot publication.

        The submitted set hash must match current durable state, the operator
        must explicitly confirm the read, and the stored credential binding
        must match the captured schema. A successful enqueue leaves any
        previous frozen source version current until the background job
        publishes every selected model atomically.
        """

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "selection_id", "selection_hash", "confirm_capture"},
        )
        assessment_evidence = request.session.get(
            _ODOO_CAPTURE_ASSESSMENT_SESSION_KEY
        )

        def prepare_capture():
            """Validate locally and release database owners before enqueueing."""

            workspace_state = context.queries.get(workspace_id)
            access_context = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.SOURCE_CAPTURE,
            )
            workspace = context.migration_workspaces.get(
                workspace_id,
                actor=context.actor,
            )
            data_version = context.data_versions.get(
                workspace.data_version_id,
                actor=context.actor,
            )
            if data_version.state is not DataVersionState.DRAFT:
                return workspace.project_id
            selections = context.queries.get_current_odoo_capture_selections(
                workspace_id
            )
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            relationship_scope = (
                context.queries.get_current_odoo_relationship_scope(
                    workspace_id
                )
            )
            required_models = (
                relationship_scope.required_capture_models
                if relationship_scope is not None
                else frozenset(
                    item.name for item in (schema.models if schema else ())
                )
            )
            if (
                not selections
                or schema is None
                or {item.model for item in selections}
                != required_models
                or not required_models.issubset(
                    item.name for item in schema.models
                )
            ):
                raise WorkspaceError(
                    "Save a capture plan for every selected Odoo record type "
                    "before freezing records."
                )
            context.odoo_source_capture.require_complete_relationship_scope(
                workspace_id,
                actor=context.actor,
            )
            selection_set_hash = odoo_capture_selection_set_hash(selections)
            submitted_hash = _text(form, "selection_hash")
            legacy_selection = selections[0] if len(selections) == 1 else None
            if submitted_hash not in {
                selection_set_hash,
                legacy_selection.content_hash if legacy_selection else "",
            }:
                raise WorkspaceError(
                    "This page is out of date. Reload and confirm the current Odoo capture plans."
                )
            if _text(form, "confirm_capture") != "1":
                raise WorkspaceError(
                    "Confirm that this read-only action may contact Odoo."
                )
            credential = get_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.READ,
            )
            if credential is None:
                raise WorkspaceError(
                    "Save a read-only Odoo API key before freezing source records."
                )
            if (
                schema.read_credential_binding_hash != credential.binding_hash
            ):
                raise WorkspaceError(
                    "The Odoo read credential changed. Refresh the record types "
                    "and fields before freezing records."
                )
            if schema.pending_refresh is not None:
                raise WorkspaceError(
                    "Odoo fields changed. Review the checked Odoo changes before "
                    "freezing another source version."
                )
            if any(
                selection.protected_filter_artifact_hash is not None
                for selection in selections
            ):
                context.odoo_source_capture.validate_current_plans(
                    workspace_id, actor=context.actor
                )
            else:
                for selection in selections:
                    plan_odoo_source_capture(selection, schema)
            expected_assessment_items = [
                {
                    "model": item.model,
                    "selection_hash": item.content_hash,
                }
                for item in selections
            ]
            evidence_items = (
                assessment_evidence.get("items")
                if isinstance(assessment_evidence, dict)
                else None
            )
            if (
                not isinstance(assessment_evidence, dict)
                or assessment_evidence.get("workspace_id") != workspace_id
                or assessment_evidence.get("selection_hash")
                != selection_set_hash
                or isinstance(assessment_evidence.get("matching_rows"), bool)
                or not isinstance(assessment_evidence.get("matching_rows"), int)
                or not 0
                <= assessment_evidence["matching_rows"]
                <= sum(item.max_rows for item in selections)
                or not isinstance(evidence_items, list)
                or [
                    {
                        "model": item.get("model"),
                        "selection_hash": item.get("selection_hash"),
                    }
                    for item in evidence_items
                    if isinstance(item, dict)
                ]
                != expected_assessment_items
                or any(
                    not isinstance(item, dict)
                    or isinstance(item.get("matching_rows"), bool)
                    or not isinstance(item.get("matching_rows"), int)
                    or item["matching_rows"] < 0
                    or item["matching_rows"]
                    > selections[index].max_rows
                    or item.get("page_size") != selections[index].page_size
                    for index, item in enumerate(evidence_items or ())
                )
                or assessment_evidence["matching_rows"]
                != sum(item["matching_rows"] for item in evidence_items)
            ):
                raise WorkspaceError(
                    "Check the current number of matching records before freezing them."
                )
            migration_project = context.migration_projects.get(
                workspace.project_id,
                actor=context.actor,
            )
            return (
                workspace_state,
                credential,
                access_context,
                assessment_evidence["matching_rows"],
                migration_project.display_name,
            )

        try:
            prepared = await run_local_operation(prepare_capture)
            if isinstance(prepared, str):
                return RedirectResponse(
                    f"/projects/{prepared}/odoo-source-restarts/"
                    f"{workspace_id}?resume=source",
                    status_code=307,
                )
            (
                workspace_state,
                credential,
                access_context,
                matching_rows,
                migration_project_name,
            ) = prepared
            gateway = context.source_capture_factory(workspace_state, credential.secret)
            job = _odoo_capture_manager(context).enqueue(
                workspace_id,
                migration_project_name,
                matching_rows,
                gateway,
                access_context=access_context,
                actor=context.actor,
            )
            request.session.pop(_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY, None)
        except (
            ConnectorError,
            MigrationFoundationError,
            OdooCaptureJobStateError,
            OdooSourceCaptureConfigurationError,
            OdooSourceCaptureError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            def render_error():
                return _render_odoo_capture_selection(
                    request,
                    context,
                    context.queries.get(workspace_id),
                    error=str(error),
                    status_code=422,
                )

            return await run_page_read(render_error)
        _flash(request, "Odoo capture started. The previous frozen version remains current until this one is complete.")
        return RedirectResponse(
            _odoo_capture_progress_url(workspace_id, job.job_id),
            status_code=303,
        )

    @router.get(
        "/workspaces/{workspace_id}/sources/odoo-capture/{job_id}",
        response_class=HTMLResponse,
    )
    async def odoo_capture_progress(
        request: Request,
        workspace_id: str,
        job_id: str,
    ):
        """Render one workspace-scoped background capture job."""

        require_session(request)
        return _render_odoo_capture_progress(
            request,
            _get_odoo_capture_job(context, workspace_id, job_id),
        )

    @router.get("/workspaces/{workspace_id}/sources/odoo-capture/{job_id}/status")
    async def odoo_capture_status(request: Request, workspace_id: str, job_id: str):
        """Return the bounded polling projection for one capture job."""

        require_session(request)
        return JSONResponse(
            _odoo_capture_job_payload(
                _get_odoo_capture_job(context, workspace_id, job_id)
            )
        )

    @router.post("/workspaces/{workspace_id}/sources/odoo-capture/{job_id}/cancel")
    async def cancel_odoo_capture(request: Request, workspace_id: str, job_id: str):
        """Request cooperative cancellation after the current bounded page.

        Cancellation does not imply rollback of an already published version;
        publication rules in the job service determine whether a candidate can
        become current.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            _odoo_capture_manager(context).cancel(workspace_id, job_id)
        except OdooCaptureJobNotFoundError as error:
            raise HTTPException(
                status_code=404,
                detail="Odoo capture job not found",
            ) from error
        _flash(request, "Impodo will stop after the current bounded Odoo page.")
        return RedirectResponse(
            _odoo_capture_progress_url(workspace_id, job_id),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/inspect")
    async def inspect_workspace_sources(request: Request, workspace_id: str):
        """Reinspect registered source bytes and replace their catalogues.

        Parsing runs in a worker thread. The inspection service rechecks stored
        evidence and publishes catalogues; this route reloads all visible
        project state after a recoverable structural failure.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        workspace_state = context.queries.get(workspace_id)
        try:
            catalogs = await run_in_threadpool(
                context.inspections.inspect_project,
                workspace_id,
                actor=context.actor,
            )
        except (SourceInspectionError, MigrationFoundationError) as error:
            return _render_file_sources(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Checked {len(catalogs)} source file(s).",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/sources#source-files",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/sources/{file_id}/configure")
    async def configure_workspace_source(
        request: Request,
        workspace_id: str,
        file_id: str,
    ):
        """Refresh one file preview and optionally confirm selected tables.

        Allowed fields are derived from the current catalogue so stale or
        injected table controls fail closed. Reinspection applies the proposed
        CSV or worksheet-header options first; confirmation then retains only
        table keys present in the refreshed catalogue and delegates warning
        acknowledgement to the source-workspace service.
        """

        form = await request.form()
        catalogs = context.queries.get_source_catalogs(workspace_id)
        catalog = next(
            (item for item in catalogs if item.file_id == file_id),
            None,
        )
        if catalog is None:
            raise HTTPException(status_code=404, detail="Source catalog not found")
        allowed = {
            "csrf_token",
            "action",
            "encoding",
            "delimiter",
            "warnings_acknowledged",
        }
        for index, _table in enumerate(catalog.tables):
            allowed.update({f"selected_{index}", f"header_row_{index}"})
        _secure_form(request, form, allowed)
        worksheet_rows: list[tuple[str, int]] = []
        selected: list[str] = []
        try:
            for index, table in enumerate(catalog.tables):
                header_text = _text(form, f"header_row_{index}").strip()
                if table.kind == "WORKSHEET" and header_text:
                    worksheet_rows.append((table.table_key, int(header_text)))
                if _text(form, f"selected_{index}"):
                    selected.append(table.table_key)
            header_row = int(_text(form, "header_row_0") or "1")
            options = SourceInspectionOptions(
                encoding=_text(form, "encoding").strip() or None,
                delimiter=_decode_delimiter(_text(form, "delimiter")),
                csv_header_row=header_row,
                worksheet_header_rows=tuple(worksheet_rows),
            )
            refreshed = await run_in_threadpool(
                context.inspections.inspect_file,
                workspace_id,
                file_id,
                options=options,
                actor=context.actor,
            )
            if _text(form, "action") == "confirm":
                refreshed_keys = {table.table_key for table in refreshed.tables}
                selected = [key for key in selected if key in refreshed_keys]
                context.sources.confirm_source(
                    workspace_id,
                    file_id,
                    selected_table_keys=selected,
                    warnings_acknowledged=bool(
                        _text(form, "warnings_acknowledged")
                    ),
                    actor=context.actor,
                )
                _flash(request, f"Confirmed {refreshed.display_name}.")
            else:
                _flash(request, f"Updated preview for {refreshed.display_name}.")
        except (SourceInspectionError, WorkspaceError, ValueError) as error:
            return _render_file_sources(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/sources#source-{file_id}",
            status_code=303,
        )

    @router.get("/workspaces/{workspace_id}/datasets", response_class=HTMLResponse)
    async def workspace_datasets(request: Request, workspace_id: str):
        """Redirect unfinished choices to Source data or show saved tables."""

        require_session(request)
        return await run_page_read(render_datasets, request, workspace_id)

    def render_datasets(request: Request, workspace_id: str):
        """Read saved tables and render while sharing bounded database owners."""

        source_page = context.queries.get_source_page(workspace_id)
        workspace_state = source_page.workspace_state
        selection = source_page.selection
        if selection is None:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources#table-choices",
                status_code=303,
            )
        return _render(
            request,
            "workspace_datasets.html",
            workspace_state=workspace_state,
            choices=(),
            selection=selection,
        )

    @router.post("/workspaces/{workspace_id}/datasets/freeze")
    async def freeze_workspace_datasets(request: Request, workspace_id: str):
        """Freeze confirmed tables under stable, user-selected dataset names.

        The source-workspace service revalidates current catalogues,
        configurations, hashes, dataset-name uniqueness, and publication
        atomicity. Expensive snapshot materialization runs off the event loop.
        """

        form = await request.form()
        choices = _dataset_choices(context, workspace_id)
        allowed = {"csrf_token"} | {
            f"dataset_name_{index}" for index, _choice in enumerate(choices)
        }
        _secure_form(request, form, allowed)
        names = {
            (choice["file_id"], choice["table_key"]): _text(
                form, f"dataset_name_{index}"
            )
            for index, choice in enumerate(choices)
        }
        try:
            await run_in_threadpool(
                context.data_version_source_projection.finalize_file_selection,
                workspace_id,
                dataset_names=names,
                actor=context.actor,
            )
        except (MigrationFoundationError, WorkspaceError) as error:
            return _render(
                request,
                "workspace_datasets.html",
                workspace_state=context.queries.get(workspace_id),
                choices=choices,
                selection=context.queries.get_source_selection(workspace_id),
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Saved the table choices.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/datasets#tables-ready",
            status_code=303,
        )

    return router


def _source_groups(catalogs):
    """Group selectable Excel regions under their physical worksheet.

    This is an in-memory presentation transform over already bounded
    catalogues. It preserves each table's original index because form field
    names are index-based, and it performs no repository or source-file reads.
    """

    result = {}
    for catalog in catalogs:
        indexed = tuple(enumerate(catalog.tables))
        groups = []
        claimed: set[int] = set()
        for index, table in indexed:
            if table.kind == "NAMED_TABLE":
                continue
            regions = tuple(
                {"index": candidate_index, "table": candidate}
                for candidate_index, candidate in indexed
                if candidate.kind == "NAMED_TABLE"
                and candidate.worksheet_name == table.name
            )
            claimed.update(item["index"] for item in regions)
            claimed.add(index)
            groups.append(
                {
                    "primary_index": index,
                    "primary": table,
                    "regions": regions,
                }
            )
        for index, table in indexed:
            if index not in claimed:
                groups.append(
                    {
                        "primary_index": index,
                        "primary": table,
                        "regions": (),
                    }
                )
        result[catalog.file_id] = tuple(groups)
    return result


def _render_file_sources(
    request: Request,
    context: WebContext,
    workspace_id: str,
    *,
    source_page: SourceReviewPage | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    """Render file review and the final table-choice action from one snapshot."""

    source_page = source_page or context.queries.get_source_page(workspace_id)
    current_workspace_state = source_page.workspace_state
    catalogs = source_page.catalogs
    configurations = source_page.configurations
    selection = source_page.selection
    return _render(
        request,
        "workspace_sources.html",
        workspace_state=current_workspace_state,
        catalogs=catalogs,
        configurations={item.file_id: item for item in configurations},
        source_groups=_source_groups(catalogs),
        choices=_dataset_choices_from(catalogs, configurations),
        selection=selection,
        can_remove_source_files=selection is None,
        error=error,
        status_code=status_code,
    )


def _render_source_file_error(
    request: Request,
    context: WebContext,
    workspace_id: str,
    return_to: str,
    error: Exception,
):
    """Return the selected source page with fresh state after a file error.

    Mutating intake commands can advance the workspace revision or catalogue
    state before another browser tab submits. Requerying here prevents the
    rejected form from redisplaying stale revision-controlled values.
    """

    workspace_state = context.queries.get(workspace_id)
    if return_to == "files":
        return _render(
            request,
            "workspace_files.html",
            workspace_state=workspace_state,
            error=str(error),
            status_code=422,
        )
    if return_to == "sources":
        return _render_file_sources(
            request,
            context,
            workspace_id,
            error=str(error),
            status_code=422,
        )
    return _render(
        request,
        "workspace_datasets.html",
        workspace_state=workspace_state,
        choices=_dataset_choices(context, workspace_id),
        selection=context.queries.get_source_selection(workspace_id),
        error=str(error),
        status_code=422,
    )


def _render_odoo_capture_selection(
    request: Request,
    context: WebContext,
    workspace_state,
    *,
    error: str | None = None,
    status_code: int = 200,
    assessment=None,
    access_refresh_required: bool = False,
):
    """Render current Odoo capture choices, credential state, and history.

    Eligible fields come only from captured schema evidence and the closed
    source-field type policy. The helper excludes Odoo's numeric ``id`` and
    volatile ``write_date`` from source values, retains a selection only when
    its model is still selected, and compares credential bindings without
    exposing the secret. Provenance reads are workspace-level, not per record.
    """

    schema = context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
    current_selections = context.queries.get_current_odoo_capture_selections(
        workspace_state.workspace_id
    )
    relationship_scope = context.queries.get_current_odoo_relationship_scope(
        workspace_state.workspace_id
    )
    current_by_model = {item.model: item for item in current_selections}
    models = tuple(schema.models) if schema is not None else ()
    model_labels_by_name = {item.name: item.label for item in models}
    selected_models = {item.name for item in models}
    model_catalog = context.queries.get_odoo_model_catalog(
        workspace_state.workspace_id
    )
    model_catalog_labels = {
        item.name: item.label
        for item in (
            model_catalog.models if model_catalog is not None else ()
        )
    }
    relationship_root_labels = tuple(
        model_catalog_labels.get(model, model)
        for model in (
            relationship_scope.root_models
            if relationship_scope is not None
            else ()
        )
    )
    related_data_suggestions = relationship_expansion_suggestions(
        propose_related_odoo_data(
            models,
            include_selected=True,
        ),
        relationship_scope,
    )
    related_data_scope = build_related_data_scope_view(
        workspace_state.workspace_id,
        related_data_suggestions,
        model_labels={
            item.name: item.label
            for item in (
                model_catalog.models if model_catalog is not None else ()
            )
        },
        selected_models=(
            relationship_scope.included_models
            if relationship_scope is not None
            else frozenset(workspace_state.intended_models)
        ),
        available_models=frozenset(
            item.name
            for item in (
                model_catalog.models if model_catalog is not None else ()
            )
        ),
        relationship_scope=relationship_scope,
    )
    guided_capture_issues = ()
    guided_capture_drafts = ()
    if (
        schema is not None
        and relationship_scope is not None
        and related_data_scope.complete
    ):
        guided_capture_proposal = propose_guided_odoo_capture_plans(
            schema,
            relationship_scope,
            current_selections=current_selections,
        )
        guided_capture_issues = guided_capture_proposal.issues
        guided_capture_drafts = guided_capture_proposal.drafts
    guided_capture_issue_by_model = {
        item.model: item
        for item in guided_capture_issues
        if item.model is not None
    }
    linked_models = {
        item.model for item in current_selections
        if item.capture_role is OdooCaptureRole.LINKED_ONLY
    }
    root_models = {
        item.model for item in current_selections
        if item.capture_role is OdooCaptureRole.ROOT
    }
    presentation_root_models = (
        frozenset(relationship_scope.root_models)
        if relationship_scope is not None
        else frozenset(workspace_state.intended_models) | frozenset(root_models)
    )
    root_capture_selections = tuple(
        item for item in current_selections
        if item.capture_role is OdooCaptureRole.ROOT
    )
    linked_capture_selections = tuple(
        item for item in current_selections
        if item.capture_role is OdooCaptureRole.LINKED_ONLY
    )
    reviewed_relationships = (
        {
            item.identity
            for item in relationship_scope.decisions
            if item.action in {
                OdooRelationshipCaptureAction.CAPTURE_LINKED,
                OdooRelationshipCaptureAction.PRESERVE_LINKED,
                OdooRelationshipCaptureAction.MATCH_EXISTING,
            }
        }
        if relationship_scope is not None
        else None
    )
    reference_models = (
        relationship_scope.reference_models
        if relationship_scope is not None
        else frozenset()
    )
    create_if_missing_models = (
        relationship_scope.create_if_missing_models
        if relationship_scope is not None
        else frozenset()
    )
    identity_scope_fields_by_model: dict[str, frozenset[str]] = {}
    for model_name in {item.source_model for item in related_data_suggestions}:
        identity_scope_fields_by_model[model_name] = frozenset(
            item.field_name
            for item in related_data_suggestions
            if item.source_model == model_name and item.identity_scope
        )
    linked_relationships = tuple(
        (model, field)
        for model in models
        for field in model.fields
        if is_odoo_capture_relationship_field(
            field, selected_models=selected_models
        )
        and (
            reviewed_relationships is None
            or (
                (
                    model.name in root_models
                    or (
                        (
                            model.name not in reference_models
                            or field.name in identity_scope_fields_by_model.get(
                                model.name,
                                frozenset(),
                            )
                        )
                        and (
                            model.name not in create_if_missing_models
                            or field.required
                            or field.name in identity_scope_fields_by_model.get(
                                model.name,
                                frozenset(),
                            )
                        )
                    )
                )
                and (
                    (model.name, field.name) in reviewed_relationships
                    or (
                        field.relation is not None
                        and field.relation_field is not None
                        and (field.relation, field.relation_field)
                        in reviewed_relationships
                    )
                    or field.relation == model.name
                )
            )
        )
        and (field.type != "one2many" or field.relation in linked_models)
    )
    has_linked_only = bool(linked_models)
    requested_model = request.query_params.get("model", "").strip()
    named_issue_model_names = frozenset(guided_capture_issue_by_model)
    default_issue_model = next(
        (
            item.name for item in models
            if item.name in named_issue_model_names
        ),
        "",
    )
    selected_model = next(
        (
            item
            for item in models
            if item.name
            == (
                requested_model
                or default_issue_model
                or next(
                    (
                        item.model for item in current_selections
                        if item.capture_role is OdooCaptureRole.ROOT
                    ),
                    "",
                )
                or (
                    current_selections[0].model
                    if current_selections
                    else models[0].name
                )
            )
        ),
        models[0] if models else None,
    )
    current = (
        current_by_model.get(selected_model.name)
        if selected_model is not None
        else None
    )
    fields = tuple(
        sorted(
            (
                field
                for field in (selected_model.fields if selected_model else ())
                if is_odoo_capture_value_field(field)
                and field.name not in {"id", "write_date"}
            ),
            key=lambda item: (item.label.casefold(), item.name),
        )
    )
    filter_fields = tuple(
        field for field in fields
        if is_odoo_capture_filter_field(field) and field.name != "active"
    )
    selected_field_names = (
        frozenset(current.field_names)
        if current is not None and current.model == selected_model.name
        else frozenset()
    ) if selected_model is not None else frozenset()
    dataset_name_default = (
        current.dataset_name
        if current is not None and current.model == selected_model.name
        else (
            f"odoo_{selected_model.name.replace('.', '_')}"[:63]
            if selected_model is not None
            else ""
        )
    )
    capture_plan_errors: dict[str, str] = {}
    if schema is not None:
        has_protected_plans = any(
            item.protected_filter_artifact_hash is not None
            for item in current_selections
        )
        protected_plan_errors = (
            context.odoo_source_capture.review_current_plans(
                workspace_state.workspace_id, actor=context.actor,
                workspace_state=workspace_state, schema=schema,
                selections=current_selections,
            )
            if has_protected_plans
            else {}
        )
        for saved_selection in current_selections:
            try:
                if not has_protected_plans:
                    plan_odoo_source_capture(saved_selection, schema)
                elif saved_selection.model in protected_plan_errors:
                    raise OdooSourceCaptureConfigurationError(
                        protected_plan_errors[saved_selection.model]
                    )
                if (
                    not _selection_uses_current_capture_policy(saved_selection)
                    or saved_selection.page_size not in ODOO_CAPTURE_PAGE_SIZES
                ):
                    raise OdooSourceCaptureConfigurationError(
                        "The saved capture plan uses the earlier row-limit workflow"
                    )
            except (OdooSourceCaptureConfigurationError, WorkspaceError) as plan_error:
                capture_plan_errors[saved_selection.model] = str(plan_error)
    current_plan_error = (
        capture_plan_errors.get(current.model) if current is not None else None
    )
    required_models = {item.name for item in models}
    plans_complete = bool(required_models) and set(current_by_model) == required_models
    selection_set_hash = (
        odoo_capture_selection_set_hash(current_selections)
        if plans_complete
        else ""
    )
    read_credential_status = get_target_credential_status(
        context.secret_store, workspace_state, TargetCredentialRole.READ,
    )
    read_credential_present = read_credential_status.available
    if read_credential_status.support_error is not None:
        if error is None:
            error = read_credential_status.support_error
            status_code = 422
    read_credential_matches_schema = bool(
        schema is not None
        and read_credential_status.available
        and schema.read_credential_binding_hash == read_credential_status.binding_hash
    )
    capture_ready_to_assess = bool(
        plans_complete
        and related_data_scope.complete
        and not capture_plan_errors
        and not access_refresh_required
        and schema is not None
        and not schema.pending_refresh
        and schema.origin.value == "LIVE_API"
        and schema.connection_target_hash
        and schema.read_principal_hash
        and schema.read_permission_hash
        and schema.read_context_hash
        and read_credential_matches_schema
    )
    edit_capture_plan = request.query_params.get("edit", "").strip() == "1"
    show_capture_plan_editor = bool(
        not plans_complete
        or capture_plan_errors
        or edit_capture_plan
    )
    all_model_names = frozenset(item.name for item in models)
    explicitly_edited_model = (
        requested_model
        if edit_capture_plan and requested_model in all_model_names
        else ""
    )
    if guided_capture_issues:
        capture_editor_model_names = (
            named_issue_model_names | frozenset(capture_plan_errors)
        ) & all_model_names
        if explicitly_edited_model:
            capture_editor_model_names |= frozenset(
                {explicitly_edited_model}
            )
    elif capture_plan_errors:
        capture_editor_model_names = (
            frozenset(capture_plan_errors) & all_model_names
        )
        if explicitly_edited_model:
            capture_editor_model_names |= frozenset(
                {explicitly_edited_model}
            )
    elif plans_complete and edit_capture_plan:
        capture_editor_model_names = frozenset(
            {requested_model}
            if requested_model in all_model_names
            else presentation_root_models & all_model_names
        )
    else:
        capture_editor_model_names = all_model_names
    capture_editor_root_models = tuple(
        item for item in models
        if item.name in capture_editor_model_names
        and item.name in presentation_root_models
    )
    capture_editor_linked_models = tuple(
        item for item in models
        if item.name in capture_editor_model_names
        and item.name not in presentation_root_models
    )
    capture_editor_linked_model_names = frozenset(
        item.name for item in capture_editor_linked_models
    )
    saved_root_models = tuple(
        item for item in models
        if item.name in current_by_model
        and current_by_model[item.name].capture_role is OdooCaptureRole.ROOT
    )
    saved_linked_models = tuple(
        item for item in models
        if item.name in current_by_model
        and current_by_model[item.name].capture_role
        is OdooCaptureRole.LINKED_ONLY
    )
    current_manifests = context.odoo_provenance.current_manifests(
        workspace_state.workspace_id,
        actor=context.actor,
    )
    current_manifest = current_manifests[0] if current_manifests else None
    capture_history = tuple(
        reversed(
            context.odoo_provenance.history(
                workspace_state.workspace_id,
                actor=context.actor,
            )
        )
    )
    return _render(
        request,
        "workspace_odoo_capture_selection.html",
        workspace_state=workspace_state,
        _read_credential_status=(workspace_state, read_credential_status),
        schema=schema,
        models=models,
        model_labels_by_name=model_labels_by_name,
        selected_model=selected_model,
        fields=fields,
        filter_fields=filter_fields,
        selected_field_names=selected_field_names,
        dataset_name_default=dataset_name_default,
        current=current,
        selected_plan_is_root=(
            selected_model is not None
            and selected_model.name in presentation_root_models
        ),
        current_selections=current_selections,
        root_capture_selections=root_capture_selections,
        linked_capture_selections=linked_capture_selections,
        current_by_model=current_by_model,
        relationship_scope=relationship_scope,
        relationship_root_labels=relationship_root_labels,
        related_data_scope=related_data_scope,
        guided_capture_issues=guided_capture_issues,
        guided_capture_drafts=guided_capture_drafts,
        guided_capture_can_prepare=(
            bool(guided_capture_drafts) or relationship_scope is None
        ),
        guided_capture_issue_by_model=guided_capture_issue_by_model,
        linked_relationships=linked_relationships,
        has_linked_only=has_linked_only,
        plans_complete=plans_complete,
        selection_set_hash=selection_set_hash,
        capture_plan_errors=capture_plan_errors,
        current_plan_error=current_plan_error,
        current_manifest=current_manifest,
        current_manifests=current_manifests,
        current_manifest_ids=frozenset(
            item.manifest_id for item in current_manifests
        ),
        capture_history=capture_history,
        read_credential_present=read_credential_present,
        read_credential_matches_schema=read_credential_matches_schema,
        capture_ready_to_assess=capture_ready_to_assess,
        show_capture_plan_editor=show_capture_plan_editor,
        capture_editor_model_names=capture_editor_model_names,
        capture_editor_root_models=capture_editor_root_models,
        capture_editor_linked_models=capture_editor_linked_models,
        capture_editor_linked_model_names=capture_editor_linked_model_names,
        saved_root_models=saved_root_models,
        saved_linked_models=saved_linked_models,
        capture_policy=_display_capture_policy(schema),
        capture_page_sizes=ODOO_CAPTURE_PAGE_SIZES,
        assessment=assessment,
        access_refresh_required=access_refresh_required,
        error=error,
        status_code=status_code,
    )


def _odoo_capture_manager(context: WebContext):
    """Return the configured job manager or fail with a workflow-state error."""

    if context.odoo_capture_jobs is None:
        raise OdooCaptureJobStateError("Background Odoo captures are unavailable")
    return context.odoo_capture_jobs


def _selection_uses_current_capture_policy(selection) -> bool:
    policy = odoo_source_policy_from_hash(selection.policy_hash)
    return policy is not None and selection.max_rows == policy.max_rows


def _display_capture_policy(schema):
    if schema is None:
        return CURRENT_ODOO_SOURCE_POLICY
    return (
        odoo_source_policy_from_hash(schema.policy_hash)
        or CURRENT_ODOO_SOURCE_POLICY
    )


def _get_odoo_capture_job(
    context: WebContext,
    workspace_id: str,
    job_id: str,
) -> OdooCaptureJob:
    """Return one job only when it belongs to the requested project."""

    try:
        return _odoo_capture_manager(context).get(workspace_id, job_id)
    except OdooCaptureJobNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail="Odoo capture job not found",
        ) from error


def _odoo_capture_progress_url(workspace_id: str, job_id: str) -> str:
    """Build the canonical browser URL for one capture job."""

    return f"/workspaces/{workspace_id}/sources/odoo-capture/{job_id}"


def _render_odoo_capture_progress(request: Request, job: OdooCaptureJob):
    """Render progress from the immutable public fields of a capture job."""

    return _render(
        request,
        "workspace_odoo_capture_progress.html",
        workspace_state=SimpleNamespace(
            workspace_id=job.workspace_id,
            name=job.migration_project_name,
            registered_at=True,
        ),
        workspace_navigation=build_odoo_capture_workspace_navigation(job),
        migration_context={
            "project_id": job.access_context.project_id,
            "data_version_id": job.access_context.data_version_id,
            "migration_run_id": job.access_context.migration_run_id,
            "workspace_id": job.access_context.workspace_id,
        },
        job=job,
        failure_message=job.failure_message,
    )


def _odoo_capture_job_payload(job: OdooCaptureJob) -> dict[str, object]:
    """Serialize safe polling fields and a success-only workflow redirect."""

    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "phase": job.phase.value,
        "message": job.message,
        "completed_rows": job.completed_rows,
        "total_rows": job.total_rows,
        "page_count": job.page_count,
        "relationship_check_page_count": job.relationship_check_page_count,
        "response_bytes": job.response_bytes,
        "normalized_bytes": job.normalized_bytes,
        "progress_percent": job.progress_percent,
        "cancel_requested": job.cancel_requested,
        "failure_message": job.failure_message,
        "redirect_url": (
            f"/workspaces/{job.workspace_id}/sources#current-capture"
            if job.status is OdooCaptureJobStatus.SUCCEEDED
            else ""
        ),
    }
