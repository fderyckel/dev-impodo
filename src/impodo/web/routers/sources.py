"""Sources browser routes."""

from __future__ import annotations
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...inspection import SourceInspectionError, SourceInspectionOptions
from ...projects import ProjectStatus
from ...workspace import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..legacy_support import _render, _secure_form, _text, _decode_delimiter, _dataset_choices, _flash


def build_sources_router(context: WebContext) -> APIRouter:
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
        return _render(
            request,
            "project_sources.html",
            project=project,
            catalogs=context.queries.get_source_catalogs(project_id),
            configurations={
                item.file_id: item
                for item in context.queries.get_source_configurations(project_id)
            },
        )

    @router.post("/projects/{project_id}/sources/inspect")
    async def inspect_project_sources(request: Request, project_id: str):
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
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Checked {len(catalogs)} source file(s).",
        )
        return RedirectResponse(
            f"/projects/{project_id}/sources",
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
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{project_id}/sources",
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
            selection = context.sources.freeze_selection(
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
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    return router
