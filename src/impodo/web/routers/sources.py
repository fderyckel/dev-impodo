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
read bounded project-level projections, but must not add an Odoo call or a
repository query per source row, field, or captured record.

See ``docs/developer/workflow/01-source-data.md``,
``docs/developer/contracts/evidence-lifecycle.md``, and ``tests/test_web_app.py``.
"""

from __future__ import annotations
from types import SimpleNamespace
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from ...application.odoo_capture_job_service import (
    OdooCaptureJobNotFoundError,
    OdooCaptureJobStateError,
)
from ...connectors import ConnectorError
from ...domain.odoo_capture import ODOO_CAPTURE_FIELD_TYPES
from ...odoo_capture_jobs import OdooCaptureJob, OdooCaptureJobStatus
from ...domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...data_versions import DataVersionState
from ...inspection import SourceInspectionError, SourceInspectionOptions
from ...migration_foundation import MigrationFoundationError
from ...projects import ProjectError, ProjectStatus, SourceMode
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.schema import _dataset_choices, _decode_delimiter
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    store_target_credential,
)


def build_sources_router(context: WebContext) -> APIRouter:
    """Build file-source and Odoo-source routes from one application context.

    The returned router is intentionally thin: it translates HTTP input and
    output while ``WebContext`` services enforce revisions, evidence bindings,
    source-mode restrictions, bounded capture policy, and publication rules.
    """

    router = APIRouter()

    @router.get("/workspaces/{project_id}/sources", response_class=HTMLResponse)
    async def project_sources(request: Request, project_id: str):
        """Render the current source-mode page or its active capture job.

        Draft projects return to setup. Registered Odoo-source projects resume
        the one active background capture when present; file projects render
        catalogues and configurations and expose file removal only before a
        frozen source selection exists.
        """

        require_session(request)
        project = context.queries.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            return RedirectResponse(
                f"/workspaces/{project.project_id}/details",
                status_code=303,
            )
        if project.source_mode is SourceMode.ODOO:
            active = (
                context.odoo_capture_jobs.active(project_id)
                if context.odoo_capture_jobs is not None
                else None
            )
            if active is not None:
                return RedirectResponse(
                    _odoo_capture_progress_url(project_id, active.job_id),
                    status_code=303,
                )
            return _render_odoo_capture_selection(request, context, project)
        catalogs = context.queries.get_source_catalogs(project_id)
        return _render(
            request,
            "project_sources.html",
            project=project,
            catalogs=catalogs,
            configurations={
                item.file_id: item
                for item in context.queries.get_source_configurations(project_id)
            },
            source_groups=_source_groups(catalogs),
            can_remove_source_files=(
                context.queries.get_source_selection(project_id) is None
            ),
        )

    @router.post("/workspaces/{project_id}/files/{file_id}/remove")
    async def remove_project_source_file(
        request: Request,
        project_id: str,
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
            removed = await run_in_threadpool(
                context.intake.remove,
                project_id,
                file_id,
                actor=context.actor,
                expected_revision=_revision(form),
            )
        except ProjectError as error:
            return _render_source_file_error(
                request,
                context,
                project_id,
                return_to,
                error,
            )
        _flash(request, f"Removed {removed.display_name} from this project.")
        suffix = "#source-files" if return_to == "sources" else ""
        return RedirectResponse(
            f"/workspaces/{project_id}/{return_to}{suffix}",
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/sources/files")
    async def add_registered_project_source_file(
        request: Request,
        project_id: str,
    ):
        """Add one bounded CSV/XLSX file before dataset freezing.

        Upload streaming, size/type validation, hashing, contained storage, and
        audit belong to the intake service and run outside the event loop. The
        uploaded handle is closed on every success or failure path.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "source_file"})
        project = context.queries.get(project_id)
        if (
            project.status is not ProjectStatus.REGISTERED
            or project.source_mode is not SourceMode.FILE
        ):
            raise HTTPException(status_code=400, detail="Source upload is unavailable")
        upload = form.get("source_file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _render_source_file_error(
                request,
                context,
                project_id,
                "sources",
                ProjectError("Choose a CSV or XLSX file"),
            )
        try:
            added = await run_in_threadpool(
                context.intake.accept,
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                display_name=upload.filename,
                stream=upload.file,
            )
        except ProjectError as error:
            return _render_source_file_error(
                request,
                context,
                project_id,
                "sources",
                error,
            )
        finally:
            await upload.close()
        _flash(request, f"Added {added.display_name} to this project.")
        return RedirectResponse(
            f"/workspaces/{project_id}/sources#source-files",
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/sources/odoo-selection")
    async def save_odoo_capture_selection(request: Request, project_id: str):
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
                "max_rows",
            },
        )
        project = context.queries.get(project_id)
        try:
            selection = await run_in_threadpool(
                context.sources.define_odoo_capture_selection,
                project_id,
                dataset_name=_text(form, "dataset_name"),
                model=_text(form, "model"),
                field_names=tuple(form.getlist("field_names")),
                include_archived=bool(_text(form, "include_archived")),
                max_rows=_text(form, "max_rows"),
                actor=context.actor,
            )
        except WorkspaceError as error:
            return _render_odoo_capture_selection(
                request,
                context,
                project,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Saved Odoo capture plan version {selection.version}. No rows were read.",
        )
        return RedirectResponse(
            f"/workspaces/{project_id}/sources#selection-saved",
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/sources/odoo-read-credential")
    async def save_odoo_capture_credential(request: Request, project_id: str):
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
        project = context.queries.get(project_id)
        if project.source_mode is not SourceMode.ODOO:
            raise HTTPException(status_code=404, detail="Odoo source not found")
        try:
            credential = store_target_credential(
                context.secret_store,
                project,
                TargetCredentialRole.READ,
                _text(form, "read_api_key"),
                persistent=bool(_text(form, "remember_read_api_key")),
            )
            audit_stored_target_credential(
                context.projects,
                project,
                TargetCredentialRole.READ,
                credential,
                actor=context.actor,
            )
        except (ProjectError, SecretStoreError, WorkspaceError) as error:
            return _render_odoo_capture_selection(
                request,
                context,
                project,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Saved the read-only Odoo key. Refresh the record types and fields so the capture is bound to this credential.",
        )
        return RedirectResponse(
            f"/workspaces/{project_id}/schema",
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/sources/odoo-capture")
    async def start_odoo_capture(request: Request, project_id: str):
        """Confirm the exact current plan and enqueue live snapshot publication.

        The submitted selection ID and semantic hash must match current durable
        state, the operator must explicitly confirm the read, and the stored
        credential binding must match the captured schema. A successful enqueue
        leaves any previous frozen source version current until the background
        job publishes the replacement atomically.
        """

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "selection_id", "selection_hash", "confirm_capture"},
        )
        project = context.queries.get(project_id)
        try:
            workspace = context.migration_workspaces.get(
                project_id,
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
            selection = context.queries.get_current_odoo_capture_selection(project_id)
            if (
                selection is None
                or selection.selection_id != _text(form, "selection_id")
                or selection.content_hash != _text(form, "selection_hash")
            ):
                raise WorkspaceError(
                    "This page is out of date. Reload and confirm the current Odoo capture plan."
                )
            if _text(form, "confirm_capture") != "1":
                raise WorkspaceError(
                    "Confirm that this read-only action may contact Odoo."
                )
            credential = get_target_credential(
                context.secret_store,
                project,
                TargetCredentialRole.READ,
            )
            if credential is None:
                raise WorkspaceError(
                    "Save a read-only Odoo API key before freezing source records."
                )
            schema = context.queries.get_odoo_schema_catalog(project_id)
            if (
                schema is None
                or schema.read_credential_binding_hash != credential.binding_hash
            ):
                raise WorkspaceError(
                    "The Odoo read credential changed. Refresh the record types "
                    "and fields before freezing records."
                )
            gateway = context.source_capture_factory(project, credential.secret)
            manager = _odoo_capture_manager(context)
            job = manager.enqueue(
                project_id,
                project.name,
                selection.max_rows,
                gateway,
                actor=context.actor,
            )
        except (
            ConnectorError,
            OdooCaptureJobStateError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_odoo_capture_selection(
                request,
                context,
                project,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Odoo capture started. The previous frozen version remains current until this one is complete.")
        return RedirectResponse(
            _odoo_capture_progress_url(project_id, job.job_id),
            status_code=303,
        )

    @router.get(
        "/workspaces/{project_id}/sources/odoo-capture/{job_id}",
        response_class=HTMLResponse,
    )
    async def odoo_capture_progress(
        request: Request,
        project_id: str,
        job_id: str,
    ):
        """Render one project-scoped background capture job."""

        require_session(request)
        return _render_odoo_capture_progress(
            request,
            _get_odoo_capture_job(context, project_id, job_id),
        )

    @router.get("/workspaces/{project_id}/sources/odoo-capture/{job_id}/status")
    async def odoo_capture_status(request: Request, project_id: str, job_id: str):
        """Return the bounded polling projection for one capture job."""

        require_session(request)
        return JSONResponse(
            _odoo_capture_job_payload(
                _get_odoo_capture_job(context, project_id, job_id)
            )
        )

    @router.post("/workspaces/{project_id}/sources/odoo-capture/{job_id}/cancel")
    async def cancel_odoo_capture(request: Request, project_id: str, job_id: str):
        """Request cooperative cancellation after the current bounded page.

        Cancellation does not imply rollback of an already published version;
        publication rules in the job service determine whether a candidate can
        become current.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            _odoo_capture_manager(context).cancel(project_id, job_id)
        except OdooCaptureJobNotFoundError as error:
            raise HTTPException(
                status_code=404,
                detail="Odoo capture job not found",
            ) from error
        _flash(request, "Impodo will stop after the current bounded Odoo page.")
        return RedirectResponse(
            _odoo_capture_progress_url(project_id, job_id),
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/sources/inspect")
    async def inspect_project_sources(request: Request, project_id: str):
        """Reinspect registered source bytes and replace their catalogues.

        Parsing runs in a worker thread. The inspection service rechecks stored
        evidence and publishes catalogues; this route reloads all visible
        project state after a recoverable structural failure.
        """

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.queries.get(project_id)
        try:
            catalogs = await run_in_threadpool(
                context.inspections.inspect_project,
                project_id,
                actor=context.actor,
            )
        except SourceInspectionError as error:
            return _render(
                request,
                "project_sources.html",
                project=project,
                catalogs=context.queries.get_source_catalogs(project_id),
                configurations={
                    item.file_id: item
                    for item in context.queries.get_source_configurations(project_id)
                },
                source_groups=_source_groups(
                    context.queries.get_source_catalogs(project_id)
                ),
                can_remove_source_files=(
                    context.queries.get_source_selection(project_id) is None
                ),
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Checked {len(catalogs)} source file(s).",
        )
        return RedirectResponse(
            f"/workspaces/{project_id}/sources#source-files",
            status_code=303,
        )

    @router.post("/workspaces/{project_id}/sources/{file_id}/configure")
    async def configure_project_source(
        request: Request,
        project_id: str,
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
        catalogs = context.queries.get_source_catalogs(project_id)
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
                project_id,
                file_id,
                options=options,
                actor=context.actor,
            )
            if _text(form, "action") == "confirm":
                refreshed_keys = {table.table_key for table in refreshed.tables}
                selected = [key for key in selected if key in refreshed_keys]
                context.sources.confirm_source(
                    project_id,
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
            return _render(
                request,
                "project_sources.html",
                project=context.queries.get(project_id),
                catalogs=context.queries.get_source_catalogs(project_id),
                configurations={
                    item.file_id: item
                    for item in context.queries.get_source_configurations(project_id)
                },
                source_groups=_source_groups(
                    context.queries.get_source_catalogs(project_id)
                ),
                can_remove_source_files=(
                    context.queries.get_source_selection(project_id) is None
                ),
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/workspaces/{project_id}/sources#source-{file_id}",
            status_code=303,
        )

    @router.get("/workspaces/{project_id}/datasets", response_class=HTMLResponse)
    async def project_datasets(request: Request, project_id: str):
        """Render confirmed physical tables and the current frozen selection."""

        require_session(request)
        project = context.queries.get(project_id)
        choices = _dataset_choices(context, project_id)
        return _render(
            request,
            "project_datasets.html",
            project=project,
            choices=choices,
            selection=context.queries.get_source_selection(project_id),
        )

    @router.post("/workspaces/{project_id}/datasets/freeze")
    async def freeze_project_datasets(request: Request, project_id: str):
        """Freeze confirmed tables under stable, user-selected dataset names.

        The source-workspace service revalidates current catalogues,
        configurations, hashes, dataset-name uniqueness, and publication
        atomicity. Expensive snapshot materialization runs off the event loop.
        """

        form = await request.form()
        choices = _dataset_choices(context, project_id)
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
                project_id,
                dataset_names=names,
                actor=context.actor,
            )
            await run_in_threadpool(
                context.data_version_source_projection.accept_file_selection,
                project_id,
                selection,
                actor=context.actor,
            )
        except (MigrationFoundationError, WorkspaceError) as error:
            return _render(
                request,
                "project_datasets.html",
                project=context.queries.get(project_id),
                choices=choices,
                selection=context.queries.get_source_selection(project_id),
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Saved the table choices.",
        )
        return RedirectResponse(
            f"/workspaces/{project_id}/datasets#tables-ready",
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


def _render_source_file_error(
    request: Request,
    context: WebContext,
    project_id: str,
    return_to: str,
    error: Exception,
):
    """Return the selected source page with fresh state after a file error.

    Mutating intake commands can advance the project revision or catalogue
    state before another browser tab submits. Requerying here prevents the
    rejected form from redisplaying stale revision-controlled values.
    """

    project = context.queries.get(project_id)
    if return_to == "files":
        return _render(
            request,
            "project_files.html",
            project=project,
            error=str(error),
            status_code=422,
        )
    if return_to == "sources":
        catalogs = context.queries.get_source_catalogs(project_id)
        return _render(
            request,
            "project_sources.html",
            project=project,
            catalogs=catalogs,
            configurations={
                item.file_id: item
                for item in context.queries.get_source_configurations(project_id)
            },
            source_groups=_source_groups(catalogs),
            can_remove_source_files=(
                context.queries.get_source_selection(project_id) is None
            ),
            error=str(error),
            status_code=422,
        )
    return _render(
        request,
        "project_datasets.html",
        project=project,
        choices=_dataset_choices(context, project_id),
        selection=context.queries.get_source_selection(project_id),
        error=str(error),
        status_code=422,
    )


def _render_odoo_capture_selection(
    request: Request,
    context: WebContext,
    project,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    """Render current Odoo capture choices, credential state, and history.

    Eligible fields come only from captured schema evidence and the closed
    source-field type policy. The helper excludes Odoo's numeric ``id`` and
    volatile ``write_date`` from source values, retains a selection only when
    its model is still selected, and compares credential bindings without
    exposing the secret. Provenance reads are project-level, not per record.
    """

    schema = context.queries.get_odoo_schema_catalog(project.project_id)
    current = context.queries.get_current_odoo_capture_selection(
        project.project_id
    )
    models = tuple(schema.models) if schema is not None else ()
    requested_model = request.query_params.get("model", "").strip()
    selected_model = next(
        (
            item
            for item in models
            if item.name
            == (
                requested_model
                or (current.model if current is not None else models[0].name)
            )
        ),
        models[0] if models else None,
    )
    fields = tuple(
        sorted(
            (
                field
                for field in (selected_model.fields if selected_model else ())
                if field.type in ODOO_CAPTURE_FIELD_TYPES
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
    try:
        read_credential = get_target_credential(
            context.secret_store,
            project,
            TargetCredentialRole.READ,
        )
        read_credential_present = read_credential is not None
    except SecretStoreError as credential_error:
        read_credential = None
        read_credential_present = False
        if error is None:
            error = str(credential_error)
            status_code = 422
    current_manifest = context.odoo_provenance.current_manifest(
        project.project_id,
        actor=context.actor,
    )
    capture_history = tuple(
        reversed(
            context.odoo_provenance.history(
                project.project_id,
                actor=context.actor,
            )
        )
    )
    return _render(
        request,
        "project_odoo_capture_selection.html",
        project=project,
        schema=schema,
        models=models,
        selected_model=selected_model,
        fields=fields,
        selected_field_names=selected_field_names,
        dataset_name_default=dataset_name_default,
        current=current,
        current_manifest=current_manifest,
        capture_history=capture_history,
        read_credential_present=read_credential_present,
        read_credential_matches_schema=bool(
            schema is not None
            and read_credential is not None
            and schema.read_credential_binding_hash == read_credential.binding_hash
        ),
        capture_policy=CURRENT_ODOO_SOURCE_POLICY,
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
    project_id: str,
    job_id: str,
) -> OdooCaptureJob:
    """Return one job only when it belongs to the requested project."""

    try:
        return _odoo_capture_manager(context).get(project_id, job_id)
    except OdooCaptureJobNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail="Odoo capture job not found",
        ) from error


def _odoo_capture_progress_url(project_id: str, job_id: str) -> str:
    """Build the canonical browser URL for one capture job."""

    return f"/workspaces/{project_id}/sources/odoo-capture/{job_id}"


def _render_odoo_capture_progress(request: Request, job: OdooCaptureJob):
    """Render progress from the immutable public fields of a capture job."""

    return _render(
        request,
        "project_odoo_capture_progress.html",
        project=SimpleNamespace(
            project_id=job.project_id,
            name=job.project_name,
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
            f"/workspaces/{job.project_id}/sources#current-capture"
            if job.status is OdooCaptureJobStatus.SUCCEEDED
            else ""
        ),
    }
