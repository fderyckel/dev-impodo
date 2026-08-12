"""Expose Stage B inspection, confirmation, and dataset freezing.

Layer: web route. Potentially expensive source inspection runs in a worker
thread through ``SourceInspectionService``. Confirmation and freezing delegate
to ``SourceWorkspaceService``; this router does not parse source bytes or
construct persistence records itself.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...domain.odoo_capture import ODOO_CAPTURE_FIELD_TYPES
from ...domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...inspection import SourceInspectionError, SourceInspectionOptions
from ...projects import ProjectStatus, SourceMode
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.schema import _dataset_choices, _decode_delimiter


def build_sources_router(context: WebContext) -> APIRouter:
    """Build the registered-source and frozen-dataset workflow routes."""

    router = APIRouter()

    @router.get("/projects/{project_id}/sources", response_class=HTMLResponse)
    async def project_sources(request: Request, project_id: str):
        require_session(request)
        project = context.queries.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            return RedirectResponse(
                f"/projects/{project.project_id}/details",
                status_code=303,
            )
        if project.source_mode is SourceMode.ODOO:
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
        )

    @router.post("/projects/{project_id}/sources/odoo-selection")
    async def save_odoo_capture_selection(request: Request, project_id: str):
        """Save a bounded protected capture plan without reading Odoo rows."""

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
            f"/projects/{project_id}/sources#selection-saved",
            status_code=303,
        )

    @router.post("/projects/{project_id}/sources/inspect")
    async def inspect_project_sources(request: Request, project_id: str):
        """Reinspect all registered source bytes and replace their catalogs."""

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
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Checked {len(catalogs)} source file(s).",
        )
        return RedirectResponse(
            f"/projects/{project_id}/sources#source-files",
            status_code=303,
        )

    @router.post("/projects/{project_id}/sources/{file_id}/configure")
    async def configure_project_source(
        request: Request,
        project_id: str,
        file_id: str,
    ):
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
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{project_id}/sources#source-{file_id}",
            status_code=303,
        )

    @router.get("/projects/{project_id}/datasets", response_class=HTMLResponse)
    async def project_datasets(request: Request, project_id: str):
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

    @router.post("/projects/{project_id}/datasets/freeze")
    async def freeze_project_datasets(request: Request, project_id: str):
        """Freeze confirmed tables under stable, user-selected dataset names."""

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
            await run_in_threadpool(
                context.sources.freeze_selection,
                project_id,
                dataset_names=names,
                actor=context.actor,
            )
        except WorkspaceError as error:
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
            f"/projects/{project_id}/datasets#tables-ready",
            status_code=303,
        )

    return router


def _source_groups(catalogs):
    """Group selectable Excel regions under their physical worksheet."""

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


def _render_odoo_capture_selection(
    request: Request,
    context: WebContext,
    project,
    *,
    error: str | None = None,
    status_code: int = 200,
):
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
        capture_policy=CURRENT_ODOO_SOURCE_POLICY,
        error=error,
        status_code=status_code,
    )
