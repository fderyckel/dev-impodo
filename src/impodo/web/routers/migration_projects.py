"""Expose the Project business root and optional Recipe publication."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...data_versions import DataVersionPurpose
from ...migration_foundation import MigrationFoundationError
from ...project_recipes import ProjectRecipeError
from ...projects import SourceMode
from ..context import WebContext
from ..forms import _form_values, _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session


def build_migration_projects_router(context: WebContext) -> APIRouter:
    """Build Project-native list, creation, overview, and publication routes."""

    router = APIRouter()

    @router.get("/projects", response_class=HTMLResponse)
    async def project_list(request: Request):
        require_session(request)
        return _render(
            request,
            "project_list.html",
            projects=context.migration_projects.list(actor=context.actor),
            unavailable_projects=context.unavailable_projects,
        )

    @router.get("/projects/new", response_class=HTMLResponse)
    async def new_project_form(request: Request):
        require_session(request)
        return _render(
            request,
            "project_new.html",
            values={"creation_request_id": str(uuid4())},
        )

    @router.post("/projects/new")
    async def new_project(request: Request):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "creation_request_id",
                "display_name",
                "migration_purpose",
                "source_mode",
                "source_system_identity",
            },
        )
        values = _form_values(form)
        try:
            bundle = context.project_authoring.create(
                actor=context.actor,
                display_name=values.get("display_name", ""),
                migration_purpose=values.get("migration_purpose", ""),
                source_mode=values.get("source_mode", SourceMode.FILE.value),
                source_system_identity=values.get("source_system_identity", ""),
                creation_request_id=values.get("creation_request_id", ""),
            )
        except MigrationFoundationError as error:
            return _render(
                request,
                "project_new.html",
                values=values,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{bundle.project.project_id}",
            status_code=303,
        )

    @router.get("/projects/{project_id}", response_class=HTMLResponse)
    async def project_overview(request: Request, project_id: str):
        require_session(request)
        return _render_project_overview(request, context, project_id)

    @router.post("/projects/{project_id}/recipes")
    async def publish_recipe(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "operation_id",
                "data_version_id",
                "workspace_id",
                "recipe_id",
                "expected_recipe_revision",
                "display_name",
                "business_purpose",
            },
        )
        recipe_id = _text(form, "recipe_id") or None
        expected = _text(form, "expected_recipe_revision")
        try:
            published = context.recipe_publication.publish(
                project_id=project_id,
                data_version_id=_text(form, "data_version_id"),
                workspace_id=_text(form, "workspace_id"),
                recipe_id=recipe_id,
                expected_recipe_revision=(int(expected) if expected else None),
                display_name=_text(form, "display_name"),
                business_purpose=_text(form, "business_purpose"),
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (MigrationFoundationError, ProjectRecipeError, ValueError) as error:
            return _render_project_overview(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Saved {published.recipe.display_name} as Recipe v{published.revision.version}.",
        )
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    return router


def _render_project_overview(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    """Load bounded registry projections; never open one database per row."""

    project = context.migration_projects.get(project_id, actor=context.actor)
    data_versions = context.data_versions.list(project_id, actor=context.actor)
    runs = context.migration_runs.list(project_id, actor=context.actor)
    workspaces = context.migration_workspaces.list_for_project(
        project_id,
        actor=context.actor,
    )
    recipes = context.project_recipes.list(project_id, actor=context.actor)
    cutover_selection = context.cutover_plans.repository.current_selection(project_id)
    production_bindings = context.production_runs.production_runs.list_for_project(
        project_id
    )
    data_version_by_id = {
        item.data_version_id: item for item in data_versions
    }
    run_by_id = {item.migration_run_id: item for item in runs}
    authoring_workspace = next(
        (
            item
            for item in workspaces
            if item.recipe_application_id is None
            and data_version_by_id[item.data_version_id].purpose
            is DataVersionPurpose.AUTHORING
        ),
        None,
    )
    authoring_data_version = (
        next(
            (
                item
                for item in data_versions
                if item.data_version_id == authoring_workspace.data_version_id
            ),
            None,
        )
        if authoring_workspace is not None
        else None
    )
    recipe_draft = None
    if authoring_workspace is not None and authoring_data_version is not None:
        recipe_draft = context.recipe_publication.draft(
            project_id=project_id,
            data_version_id=authoring_data_version.data_version_id,
            workspace_id=authoring_workspace.workspace_id,
            actor=context.actor,
        )
    return _render(
        request,
        "project_business_overview.html",
        project=project,
        data_versions=data_versions,
        runs=runs,
        workspaces=workspaces,
        recipes=recipes,
        cutover_selection=cutover_selection,
        production_bindings=production_bindings,
        production_data_versions=data_version_by_id,
        production_runs=run_by_id,
        authoring_workspace=authoring_workspace,
        authoring_data_version=authoring_data_version,
        recipe_draft=recipe_draft,
        publication_operation_id=str(uuid4()),
        error=error,
        status_code=status_code,
    )
