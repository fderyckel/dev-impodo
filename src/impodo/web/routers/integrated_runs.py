"""Expose Project-owned integrated Test run planning and status."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...migration_foundation import MigrationFoundationError
from ...migration_run_planning import RecipeDependency
from ...recipes import RecipeError
from ...secrets import SecretStoreError
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    get_target_credential_status,
)


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
                "expected_workspace_revision",
                "export_as_of",
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
            result = context.test_runs.start_setup(
                project_id,
                expected_workspace_revision=int(
                    _text(form, "expected_workspace_revision")
                ),
                recipe_revisions=selected,
                dependencies=dependencies,
                label=_text(form, "label"),
                export_as_of=_text(form, "export_as_of"),
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (
            MigrationFoundationError,
            RecipeError,
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
                export_as_of=_text(form, "export_as_of"),
                operation_id=_text(form, "operation_id"),
            )
        _flash(
            request,
            "Created a fresh Test data version from the selected Recipe requirements.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/test-runs/"
            f"{result.run.migration_run_id}/fresh-data",
            status_code=303,
        )

    @router.get(
        "/projects/{project_id}/test-runs/{migration_run_id}/fresh-data",
        response_class=HTMLResponse,
    )
    async def test_run_fresh_data(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Show the exact Recipe-owned source needs for this Test delivery."""

        require_session(request)
        return _render(
            request,
            "project_test_run_fresh_data.html",
            **_test_fresh_data_view(
                context,
                project_id,
                migration_run_id,
            ),
        )

    @router.get(
        "/projects/{project_id}/test-runs/{migration_run_id}/activate",
        response_class=HTMLResponse,
    )
    async def test_run_activation_form(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        require_session(request)
        return _render_test_activation(
            request,
            context,
            project_id,
            migration_run_id,
        )

    @router.post("/projects/{project_id}/test-runs/{migration_run_id}/activate")
    async def activate_test_run(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_workspace_revision", "operation_id"},
        )
        try:
            view = _test_activation_view(
                context,
                project_id,
                migration_run_id,
            )
            target_schema, target_references = (
                context.run_planning.target_evidence_from_workspace(
                    project_id,
                    view["setup_workspace"].workspace_id,
                    actor=context.actor,
                )
            )
            read_credential = get_target_credential(
                context.secret_store,
                view["setup_state"],
                TargetCredentialRole.READ,
            )
            if read_credential is None:
                raise SecretStoreError(
                    "Enter and verify the read-only Odoo key for this Test run first"
                )
            result = context.test_runs.activate(
                project_id,
                migration_run_id,
                expected_workspace_revision=int(
                    _text(form, "expected_workspace_revision")
                ),
                target_schema=target_schema,
                target_reference_bundle=target_references,
                credential_generation=read_credential.binding_hash,
                operation_id=_text(form, "operation_id"),
                actor=context.actor,
            )
        except (
            MigrationFoundationError,
            RecipeError,
            SecretStoreError,
            TypeError,
            ValueError,
        ) as error:
            return _render_test_activation(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                operation_id=_text(form, "operation_id"),
            )
        _flash(
            request,
            f"Created {len(result.applications)} fresh Recipe work areas.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}",
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
        if run.purpose.value == "TEST" and run.target_binding_id is None:
            return RedirectResponse(
                f"/projects/{project_id}/test-runs/{migration_run_id}/activate",
                status_code=303,
            )
        if run.purpose.value == "PRODUCTION" and run.target_binding_id is None:
            return RedirectResponse(
                f"/projects/{project_id}/production-runs/{migration_run_id}/activate",
                status_code=303,
            )
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        progress = context.run_planning.repository.progress(migration_run_id)
        issues = context.run_planning.repository.list_run_issues(migration_run_id)
        recipes = {
            item.recipe_id: item
            for item in context.recipes.list(project_id, actor=context.actor)
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

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/applications/{application_id}"
    )
    async def continue_recipe_application(
        request: Request,
        project_id: str,
        migration_run_id: str,
        application_id: str,
    ):
        """Enter one application through the run-owned review-and-load step."""

        require_session(request)
        project = context.migration_projects.get(project_id, actor=context.actor)
        run = context.migration_runs.get(migration_run_id, actor=context.actor)
        if run.project_id != project.project_id:
            return HTMLResponse("MigrationRun not found", status_code=404)
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        application = next(
            (
                item
                for item in bundle.applications
                if item.application_id == application_id
            ),
            None,
        )
        if application is None or application.project_id != project.project_id:
            return HTMLResponse("Recipe application not found", status_code=404)
        return RedirectResponse(
            f"/workspaces/{application.workspace_id}/prepare",
            status_code=303,
        )

    @router.get("/projects/{project_id}/runs/{migration_run_id}/odoo")
    async def continue_run_odoo_check(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Return target recovery to the run's one shared setup workspace."""

        require_session(request)
        project = context.migration_projects.get(project_id, actor=context.actor)
        run = context.migration_runs.get(migration_run_id, actor=context.actor)
        if run.project_id != project.project_id:
            return HTMLResponse("MigrationRun not found", status_code=404)
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        setup_workspace = next(
            (
                item
                for item in bundle.workspaces
                if item.recipe_application_id is None
            ),
            None,
        )
        if setup_workspace is None:
            return HTMLResponse("Run setup not found", status_code=404)
        return RedirectResponse(
            f"/workspaces/{setup_workspace.workspace_id}/schema",
            status_code=303,
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
    export_as_of: str = "",
    operation_id: str | None = None,
):
    project = context.migration_projects.get(project_id, actor=context.actor)
    recipes = context.recipes.list(project_id, actor=context.actor)
    return _render(
        request,
        "project_test_run_new.html",
        project=project,
        recipes=recipes,
        operation_id=operation_id or str(uuid4()),
        selected_values=set(selected_values),
        dependency_values=set(dependency_values),
        label=label,
        export_as_of=export_as_of,
        error=error,
        status_code=status_code,
    )


def _render_test_activation(
    request,
    context,
    project_id,
    migration_run_id,
    *,
    error=None,
    status_code=200,
    operation_id=None,
):
    view = _test_activation_view(context, project_id, migration_run_id)
    return _render(
        request,
        "project_test_run_activation.html",
        **view,
        workspace_state=view["setup_state"],
        operation_id=operation_id or str(uuid4()),
        error=error,
        status_code=status_code,
    )


def _test_activation_view(context, project_id, migration_run_id):
    project = context.migration_projects.get(project_id, actor=context.actor)
    binding = context.test_runs.get(migration_run_id, actor=context.actor)
    if binding.project_id != project.project_id:
        raise MigrationFoundationError("Test run does not belong to this Project")
    run = context.migration_runs.get(migration_run_id, actor=context.actor)
    data_version = context.data_versions.get(
        binding.data_version_id, actor=context.actor
    )
    setup_workspace = context.migration_workspaces.get(
        binding.setup_workspace_id,
        actor=context.actor,
    )
    setup_state = context.workspace_states.repository.get(binding.setup_workspace_id)
    recipes = {
        item.recipe_id: item
        for item in context.recipes.list(project_id, actor=context.actor)
    }
    read_status = get_target_credential_status(
        context.secret_store,
        setup_state,
        TargetCredentialRole.READ,
    )
    target_ready = False
    target_error = "Capture the Odoo fields and supporting lists for this Test run."
    try:
        context.run_planning.target_evidence_from_workspace(
            project_id,
            setup_workspace.workspace_id,
            actor=context.actor,
        )
    except MigrationFoundationError as error:
        target_error = str(error)
    else:
        target_ready = True
        target_error = ""
    if data_version.state.value != "FROZEN":
        setup_destination = (
            f"/projects/{project_id}/test-runs/{migration_run_id}/fresh-data"
        )
        setup_action_label = "Continue fresh data"
    elif not target_ready:
        setup_destination = f"/workspaces/{setup_workspace.workspace_id}/schema"
        setup_action_label = "Continue Odoo check"
    else:
        setup_destination = (
            f"/projects/{project_id}/test-runs/{migration_run_id}/activate"
        )
        setup_action_label = "Review Test setup"
    return {
        "binding": binding,
        "data_version": data_version,
        "project": project,
        "read_status": read_status,
        "recipes": recipes,
        "run": run,
        "setup_state": setup_state,
        "setup_action_label": setup_action_label,
        "setup_destination": setup_destination,
        "setup_workspace": setup_workspace,
        "target_error": target_error,
        "target_ready": target_ready,
    }


def _test_fresh_data_view(context, project_id, migration_run_id):
    project = context.migration_projects.get(project_id, actor=context.actor)
    binding = context.test_runs.get(migration_run_id, actor=context.actor)
    if binding.project_id != project.project_id:
        raise MigrationFoundationError("Test run does not belong to this Project")
    run = context.migration_runs.get(migration_run_id, actor=context.actor)
    data_version = context.data_versions.get(
        binding.data_version_id,
        actor=context.actor,
    )
    setup_workspace = context.migration_workspaces.get(
        binding.setup_workspace_id,
        actor=context.actor,
    )
    setup_state = context.workspace_states.repository.get(
        binding.setup_workspace_id
    )
    accepted = data_version.state.value == "FROZEN"
    if accepted:
        action_href = f"/projects/{project_id}/runs/{migration_run_id}/odoo"
        action_label = "Continue to Check Odoo"
    elif setup_state.status.value == "DRAFT":
        action_href = f"/workspaces/{setup_workspace.workspace_id}/files"
        action_label = (
            "Review fresh files" if setup_state.source_files else "Add fresh files"
        )
    else:
        action_href = f"/workspaces/{setup_workspace.workspace_id}/sources"
        action_label = "Review detected tables"
    return {
        "accepted": accepted,
        "action_href": action_href,
        "action_label": action_label,
        "binding": binding,
        "data_version": data_version,
        "project": project,
        "requirements": context.test_runs.fresh_data_requirements(
            migration_run_id,
            actor=context.actor,
        ),
        "run": run,
        "setup_state": setup_state,
        "setup_workspace": setup_workspace,
    }
