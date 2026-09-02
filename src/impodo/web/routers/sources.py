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
from types import SimpleNamespace
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from impodo.domain.shared.access import Capability
from ...application.odoo_capture_job_service import (
    OdooCaptureJobNotFoundError,
    OdooCaptureJobStateError,
)
from impodo.domain.odoo.contracts import ConnectorError
from ...domain.odoo_source_capture import (
    OdooSourceCaptureAccessRefreshRequired,
    OdooSourceCaptureError,
    OdooSourceCaptureConfigurationError,
    is_odoo_capture_value_field,
    plan_odoo_source_capture,
)
from impodo.application.workspace.odoo_capture_jobs import OdooCaptureJob, OdooCaptureJobStatus
from ...domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...domain.odoo_capture import (
    ODOO_CAPTURE_PAGE_SIZES,
    odoo_capture_selection_set_hash,
)
from ...domain.data_version.models import DataVersionState
from impodo.application.data_version.inspection import SourceInspectionError, SourceInspectionOptions
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStatus,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.workspace.errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.schema import (
    _dataset_choices,
    _dataset_choices_from,
    _decode_delimiter,
)
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    store_target_credential,
)
from ..source_file_commands import accept_source_uploads, remove_source_file


_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY = "odoo_capture_assessment"


def build_sources_router(context: WebContext) -> APIRouter:
    """Build file-source and Odoo-source routes from one application context.

    The returned router is intentionally thin: it translates HTTP input and
    output while ``WebContext`` services enforce revisions, evidence bindings,
    source-mode restrictions, bounded capture policy, and publication rules.
    """

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/sources", response_class=HTMLResponse)
    async def workspace_sources(request: Request, workspace_id: str):
        """Render the current source-mode page or its active capture job.

        Draft projects return to setup. Registered Odoo-source projects resume
        the one active background capture when present; file projects render
        catalogues and configurations and expose file removal only before a
        frozen source selection exists.
        """

        require_session(request)
        workspace_state = context.queries.get(workspace_id)
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
            workspace_state=workspace_state,
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
            },
        )
        workspace_state = context.queries.get(workspace_id)
        try:
            selection = await run_in_threadpool(
                context.sources.define_odoo_capture_selection,
                workspace_id,
                dataset_name=_text(form, "dataset_name"),
                model=_text(form, "model"),
                field_names=tuple(form.getlist("field_names")),
                include_archived=bool(_text(form, "include_archived")),
                page_size=_text(form, "page_size"),
                actor=context.actor,
            )
        except WorkspaceError as error:
            return _render_odoo_capture_selection(
                request,
                context,
                workspace_state,
                error=str(error),
                status_code=422,
            )
        request.session.pop(_ODOO_CAPTURE_ASSESSMENT_SESSION_KEY, None)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        current_selections = (
            context.queries.get_current_odoo_capture_selections(workspace_id)
            if schema is not None
            else ()
        )
        planned_models = {item.model for item in current_selections}
        missing_models = tuple(
            item
            for item in (schema.models if schema is not None else ())
            if item.name not in planned_models
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
            "All Odoo capture plans are saved. Check the matching records "
            "to continue.",
        )
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
            {"csrf_token", "selection_id", "selection_hash"},
        )
        workspace_state = context.queries.get(workspace_id)
        try:
            workspace = context.migration_workspaces.get(
                workspace_id,
                actor=context.actor,
            )
            data_version = context.data_versions.get(
                workspace.data_version_id,
                actor=context.actor,
            )
            if data_version.state is not DataVersionState.DRAFT:
                raise WorkspaceError(
                    "This DataVersion already has accepted source evidence. "
                    "Start a new run with a new DataVersion for another capture."
                )
            selections = context.queries.get_current_odoo_capture_selections(
                workspace_id
            )
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            if (
                not selections
                or schema is None
                or {item.model for item in selections}
                != {item.name for item in schema.models}
            ):
                raise WorkspaceError(
                    "Save a capture plan for every selected Odoo record type "
                    "before checking matching records."
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
                selection.max_rows != CURRENT_ODOO_SOURCE_POLICY.max_rows
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
                    "Save a read-only Odoo API key before checking matching records."
                )
            if (
                schema.read_credential_binding_hash != credential.binding_hash
            ):
                raise WorkspaceError(
                    "The Odoo read credential changed. Refresh the record "
                    "types and fields first."
                )
            if schema.pending_refresh is not None:
                raise WorkspaceError(
                    "Odoo fields changed. Review the checked Odoo changes first."
                )
            gateway = context.source_capture_factory(
                workspace_state,
                credential.secret,
            )
            assessment = await run_in_threadpool(
                context.odoo_source_capture.assess_all,
                workspace_id,
                gateway,
                actor=context.actor,
            )
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
        except (
            ConnectorError,
            OdooSourceCaptureError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_odoo_capture_selection(
                request,
                context,
                workspace_state,
                error=str(error),
                access_refresh_required=isinstance(
                    error,
                    OdooSourceCaptureAccessRefreshRequired,
                ),
                status_code=422,
            )
        return _render_odoo_capture_selection(
            request,
            context,
            workspace_state,
            assessment=assessment,
        )

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
        workspace_state = context.queries.get(workspace_id)
        try:
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
                raise WorkspaceError(
                    "This DataVersion already has accepted source evidence. "
                    "Start a new run with a new DataVersion for another capture."
                )
            selections = context.queries.get_current_odoo_capture_selections(
                workspace_id
            )
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            if (
                not selections
                or schema is None
                or {item.model for item in selections}
                != {item.name for item in schema.models}
            ):
                raise WorkspaceError(
                    "Save a capture plan for every selected Odoo record type "
                    "before freezing records."
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
            for selection in selections:
                plan_odoo_source_capture(selection, schema)
            assessment_evidence = request.session.get(
                _ODOO_CAPTURE_ASSESSMENT_SESSION_KEY
            )
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
            gateway = context.source_capture_factory(workspace_state, credential.secret)
            manager = _odoo_capture_manager(context)
            workspace = context.migration_workspaces.get(
                workspace_id,
                actor=context.actor,
            )
            migration_project = context.migration_projects.get(
                workspace.project_id,
                actor=context.actor,
            )
            job = manager.enqueue(
                workspace_id,
                migration_project.display_name,
                assessment_evidence["matching_rows"],
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
            return _render_odoo_capture_selection(
                request,
                context,
                workspace_state,
                error=str(error),
                status_code=422,
            )
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
        except SourceInspectionError as error:
            return _render_file_sources(
                request,
                context,
                workspace_id,
                workspace_state=workspace_state,
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
        workspace_state = context.queries.get(workspace_id)
        selection = context.queries.get_source_selection(workspace_id)
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
            selection = await run_in_threadpool(
                context.sources.freeze_selection,
                workspace_id,
                dataset_names=names,
                actor=context.actor,
            )
            await run_in_threadpool(
                context.data_version_source_projection.accept_file_selection,
                workspace_id,
                selection,
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
    workspace_state: WorkspaceState | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    """Render file review and the final table-choice action from one snapshot."""

    current_workspace_state = workspace_state or context.queries.get(workspace_id)
    catalogs = context.queries.get_source_catalogs(workspace_id)
    configurations = context.queries.get_source_configurations(workspace_id)
    selection = context.queries.get_source_selection(workspace_id)
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
            workspace_state=workspace_state,
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
    current_by_model = {item.model: item for item in current_selections}
    models = tuple(schema.models) if schema is not None else ()
    requested_model = request.query_params.get("model", "").strip()
    selected_model = next(
        (
            item
            for item in models
            if item.name
            == (
                requested_model
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
        for saved_selection in current_selections:
            try:
                plan_odoo_source_capture(saved_selection, schema)
                if (
                    saved_selection.max_rows
                    != CURRENT_ODOO_SOURCE_POLICY.max_rows
                    or saved_selection.page_size not in ODOO_CAPTURE_PAGE_SIZES
                ):
                    raise OdooSourceCaptureConfigurationError(
                        "The saved capture plan uses the earlier row-limit workflow"
                    )
            except OdooSourceCaptureConfigurationError as plan_error:
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
    try:
        read_credential = get_target_credential(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.READ,
        )
        read_credential_present = read_credential is not None
    except SecretStoreError as credential_error:
        read_credential = None
        read_credential_present = False
        if error is None:
            error = str(credential_error)
            status_code = 422
    read_credential_matches_schema = bool(
        schema is not None
        and read_credential is not None
        and schema.read_credential_binding_hash == read_credential.binding_hash
    )
    capture_ready_to_assess = bool(
        plans_complete
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
        schema=schema,
        models=models,
        selected_model=selected_model,
        fields=fields,
        selected_field_names=selected_field_names,
        dataset_name_default=dataset_name_default,
        current=current,
        current_selections=current_selections,
        current_by_model=current_by_model,
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
        capture_policy=CURRENT_ODOO_SOURCE_POLICY,
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
