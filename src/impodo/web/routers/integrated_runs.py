"""Expose Project-owned integrated Test run planning and status."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...migration_foundation import MigrationFoundationError
from ...migration_run_planning import RecipeDependency
from ...project_recipes import ProjectRecipeError
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session


def build_integrated_runs_router(context: WebContext) -> APIRouter:
    """Build integrated Test planning and bounded run-projection routes."""

    router = APIRouter()

    @router.get(
        "/projects/{project_id}/test-runs/new",
        response_class=HTMLResponse,
    )
    async def new_test_run_form(request: Request, project_id: str):
        require_session(request)
        return _render_test_run_form(request, context, project_id)

    @router.post("/projects/{project_id}/test-runs/new")
    async def new_test_run(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "operation_id",
                "expected_project_revision",
                "data_version_id",
                "target_workspace_id",
                "label",
                "recipe_revision",
                "dependency",
            },
        )
        selected_values = tuple(str(item) for item in form.getlist("recipe_revision"))
        dependency_values = tuple(str(item) for item in form.getlist("dependency"))
        try:
            selected = tuple(
                (recipe_id, int(version))
                for recipe_id, version in (
                    value.rsplit(":", 1) for value in selected_values
                )
            )
            dependencies = tuple(
                RecipeDependency(
                    before_recipe_id=before,
                    after_recipe_id=after,
                )
                for before, after in (
                    value.split(">", 1) for value in dependency_values
                )
            )
            target_schema, target_references = (
                context.run_planning.target_evidence_from_workspace(
                    project_id,
                    _text(form, "target_workspace_id"),
                    actor=context.actor,
                )
            )
            result = context.run_planning.start_test_run(
                project_id,
                expected_project_revision=int(
                    _text(form, "expected_project_revision")
                ),
                data_version_id=_text(form, "data_version_id"),
                recipe_revisions=selected,
                dependencies=dependencies,
                target_schema=target_schema,
                target_reference_bundle=target_references,
                credential_generation=(
                    target_schema.read_credential_binding_hash
                ),
                label=_text(form, "label"),
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (
            MigrationFoundationError,
            ProjectRecipeError,
            TypeError,
            ValueError,
        ) as error:
            return _render_test_run_form(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
                selected_values=selected_values,
                dependency_values=dependency_values,
                label=_text(form, "label"),
                operation_id=_text(form, "operation_id"),
                selected_data_version_id=_text(form, "data_version_id"),
                selected_target_workspace_id=_text(
                    form,
                    "target_workspace_id",
                ),
            )
        _flash(
            request,
            (
                f"Started {result.run.label} with "
                f"{len(result.applications)} Recipe applications."
            ),
        )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{result.run.migration_run_id}",
            status_code=303,
        )

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}",
        response_class=HTMLResponse,
    )
    async def integrated_run(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        require_session(request)
        project = context.migration_projects.get(project_id, actor=context.actor)
        run = context.migration_runs.get(migration_run_id, actor=context.actor)
        if run.project_id != project.project_id:
            return HTMLResponse("MigrationRun not found", status_code=404)
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        progress = context.run_planning.repository.progress(migration_run_id)
        issues = context.run_planning.repository.list_run_issues(migration_run_id)
        recipes = {
            item.recipe_id: item
            for item in context.project_recipes.list(project_id, actor=context.actor)
        }
        applications = {item.recipe_id: item for item in bundle.applications}
        ordered_applications = tuple(
            applications[recipe_id]
            for recipe_id in bundle.requirement_plan.application_order
        )
        target_schema = context.run_planning.repository.get_run_target_schema(
            migration_run_id
        )
        plan_binding = context.cutover_plans.repository.get_run_binding(
            migration_run_id
        )
        plan_revision = context.cutover_plans.repository.get_revision(
            plan_binding.cutover_plan_id,
            plan_binding.cutover_plan_revision,
        )
        qualifications = context.cutover_plans.repository.list_qualifications(
            plan_binding.cutover_plan_id,
            plan_binding.cutover_plan_revision,
        )
        selection = context.cutover_plans.repository.current_selection(project_id)
        return _render(
            request,
            "project_integrated_run.html",
            project=project,
            bundle=bundle,
            progress=progress,
            applications=ordered_applications,
            recipes=recipes,
            issues=issues,
            target_schema=target_schema,
            plan_revision=plan_revision,
            qualification=(qualifications[0] if qualifications else None),
            selection=selection,
        )

    return router


def _render_test_run_form(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    selected_values: tuple[str, ...] = (),
    dependency_values: tuple[str, ...] = (),
    label: str = "Integrated Test run",
    operation_id: str | None = None,
    selected_data_version_id: str = "",
    selected_target_workspace_id: str = "",
):
    project = context.migration_projects.get(project_id, actor=context.actor)
    data_versions = tuple(
        item
        for item in context.data_versions.list(project_id, actor=context.actor)
        if item.purpose.value == "TEST" and item.state.value == "FROZEN"
    )
    recipes = context.project_recipes.list(project_id, actor=context.actor)
    workspaces = context.migration_workspaces.list_for_project(
        project_id,
        actor=context.actor,
    )
    target_workspaces = tuple(
        item for item in workspaces if item.recipe_application_id is None
    )
    return _render(
        request,
        "project_test_run_new.html",
        project=project,
        data_versions=data_versions,
        recipes=recipes,
        target_workspaces=target_workspaces,
        selected_data_version_id=selected_data_version_id,
        selected_target_workspace_id=selected_target_workspace_id,
        operation_id=operation_id or str(uuid4()),
        selected_values=set(selected_values),
        dependency_values=set(dependency_values),
        label=label,
        error=error,
        status_code=status_code,
    )
