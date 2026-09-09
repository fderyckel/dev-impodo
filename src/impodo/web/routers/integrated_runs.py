"""Expose Project-owned integrated Test run planning and status."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.run.target_defaults import mapped_target_defaults
from impodo.application.run.target_matches import TargetMatchReviewChanged
from impodo.application.run.progress import (
    ApplicationResumeStep, application_resume_step, next_unverified_application,
)
from impodo.domain.mapping.create_field_policy import VerifiedCreateDefaultAction
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.run.contracts import (
    MigrationRunPlanningError,
    RecipeApplicationStatus,
    RecipeDependency,
)
from ...domain.run.models import MigrationRunPurpose
from ...domain.recipe.models import RecipeError
from impodo.domain.workspace.errors import WorkspaceError
from ..context import WebContext
from ..run_urls import fresh_data_url as _fresh_data_url
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.navigation import build_recipe_run_navigation
from ..presenters.schema import _render_schema
from ..run_review import build_integrated_run_review
from ..run_commands import start_next_preparation, recover_run_preparation
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
                f"/projects/{project_id}/runs/{migration_run_id}/odoo",
                status_code=303,
            )
        if run.purpose.value == "PRODUCTION" and run.target_binding_id is None:
            return RedirectResponse(
                f"/projects/{project_id}/production-runs/{migration_run_id}/activate",
                status_code=303,
            )
        if run.purpose is MigrationRunPurpose.PRODUCTION:
            activation = await run_in_threadpool(
                context.production_runs.activation_operation, migration_run_id, actor=context.actor,
            )
            if activation is None or activation.state.value != "COMMITTED":
                return RedirectResponse(
                    f"/projects/{project_id}/production-runs/{migration_run_id}/activate", status_code=303,
                )
        try:
            await run_in_threadpool(recover_run_preparation, context, migration_run_id)
        except WorkspaceError as error:
            _flash(request, str(error))
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        progress = context.run_planning.repository.progress(migration_run_id)
        issues = context.run_planning.repository.list_run_issues(migration_run_id)
        recipe_reads = context.recipes.read_revisions(
            project_id,
            tuple(
                (item.recipe_id, item.recipe_revision)
                for item in bundle.applications
            ),
            actor=context.actor,
        )
        recipes = {
            recipe_id: item.recipe
            for (recipe_id, _version), item in recipe_reads.items()
        }
        applications = {item.recipe_id: item for item in bundle.applications}
        ordered_applications = tuple(
            applications[recipe_id]
            for recipe_id in bundle.requirement_plan.application_order
        )
        target_schema = context.run_planning.repository.get_run_target_schema(
            migration_run_id
        )
        plan_binding = (
            context.production_runs.production_runs.get(migration_run_id)
            if run.purpose is MigrationRunPurpose.PRODUCTION else
            context.cutover_plans.repository.get_run_binding(migration_run_id)
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
        review = build_integrated_run_review(
            context,
            bundle,
            recipes=recipes,
            issues=issues,
        )
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
            review=review,
            migration_context=run,
            workspace_navigation=build_recipe_run_navigation(
                project_id=project_id,
                migration_run_id=migration_run_id,
                migration_project_name=project.display_name,
                run_purpose=run.purpose.value,
                complete=bool(review.total_count) and review.completed_count == review.total_count,
                odoo_needs_attention=review.odoo_needs_attention,
            ),
        )

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/applications/"
        "{application_id}/odoo-defaults",
        response_class=HTMLResponse,
    )
    async def review_application_odoo_defaults(
        request: Request,
        project_id: str,
        migration_run_id: str,
        application_id: str,
    ):
        """Show exact verified defaults before one grouped confirmation."""

        require_session(request)
        application = context.run_planning.repository.get_application(
            application_id
        )
        if (
            application.project_id != project_id
            or application.migration_run_id != migration_run_id
        ):
            return HTMLResponse("RecipeApplication not found", status_code=404)
        schema = context.queries.get_odoo_schema_catalog(
            application.workspace_id
        )
        revision = context.mapping_workspace.mappings.get_mapping_revision(
            application.workspace_id
        )
        if schema is None or revision is None:
            return HTMLResponse(
                "Recheck Odoo before reviewing these defaults",
                status_code=422,
            )
        default_reviews = tuple(
            item
            for item in context.run_planning.repository.list_issues(
                application.application_id
            )
            if item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
            and item.level.value == "REVIEW"
        )
        if not default_reviews:
            return HTMLResponse(
                "No verified Odoo defaults are waiting for review",
                status_code=422,
            )
        defaults = tuple(
            {
                "field": item.field,
                "model": item.model,
                "value": _run_default_value_label(item.field),
                "reason": item.decision.reason,
            }
            for item in mapped_target_defaults(
                revision.definition,
                schema,
                action=VerifiedCreateDefaultAction.REQUIRE_REVIEW,
            )
        )
        if not defaults or len(defaults) != len(default_reviews):
            return HTMLResponse(
                "No verified Odoo defaults are waiting for review",
                status_code=422,
            )
        recipe = context.recipes.get(application.recipe_id, actor=context.actor)
        return _render(
            request,
            "project_recipe_odoo_defaults.html",
            application=application,
            defaults=defaults,
            target_database=schema.database,
            project_id=project_id,
            recipe=recipe,
        )

    @router.post(
        "/projects/{project_id}/runs/{migration_run_id}/applications/"
        "{application_id}/odoo-defaults"
    )
    async def confirm_application_odoo_defaults(
        request: Request,
        project_id: str,
        migration_run_id: str,
        application_id: str,
    ):
        """Confirm the already captured create defaults for one application."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        application = context.run_planning.repository.get_application(
            application_id
        )
        if (
            application.project_id != project_id
            or application.migration_run_id != migration_run_id
        ):
            return HTMLResponse("RecipeApplication not found", status_code=404)
        try:
            await run_in_threadpool(
                context.run_planning.confirm_application_odoo_defaults,
                application_id,
                actor=context.actor,
            )
        except (MigrationRunPlanningError, WorkspaceError) as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/projects/{project_id}/runs/{migration_run_id}/applications/"
                f"{application_id}/odoo-defaults",
                status_code=303,
            )
        _flash(request, "Confirmed the current Odoo defaults for this Recipe run.")
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}",
            status_code=303,
        )

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/applications/"
        "{application_id}/target-matches",
        response_class=HTMLResponse,
    )
    async def review_application_target_matches(
        request: Request,
        project_id: str,
        migration_run_id: str,
        application_id: str,
    ):
        """Show only target-specific Selection and Many2one decisions."""

        require_session(request)
        return await run_in_threadpool(
            _render_application_target_matches,
            request,
            context,
            project_id,
            migration_run_id,
            application_id,
        )

    @router.post(
        "/projects/{project_id}/runs/{migration_run_id}/applications/"
        "{application_id}/target-matches"
    )
    async def confirm_application_target_matches(
        request: Request,
        project_id: str,
        migration_run_id: str,
        application_id: str,
    ):
        """Save focused decisions, validate, and confirm the application mapping."""

        require_session(request)
        application = _run_application(
            context,
            project_id,
            migration_run_id,
            application_id,
        )
        form = await request.form()
        submitted = {str(key): str(value) for key, value in form.items()}
        try:
            allowed = {
                "csrf_token", "expected_definition_hash",
                "expected_working_draft_version", "expected_evidence_hash",
            } | {key for key in submitted if key.startswith("match_")}
            _secure_form(request, form, allowed)
            confirmed = await run_in_threadpool(
                context.recipe_target_matches.confirm,
                application,
                decisions={key: value for key, value in submitted.items() if key.startswith("match_")},
                expected_working_draft_version=int(_text(form, "expected_working_draft_version")),
                expected_definition_hash=_text(form, "expected_definition_hash"),
                expected_evidence_hash=_text(form, "expected_evidence_hash"),
                actor=context.actor,
            )
        except (MigrationRunPlanningError, WorkspaceError, ValueError) as error:
            return await run_in_threadpool(
                _render_application_target_matches,
                request,
                context,
                project_id,
                migration_run_id,
                application_id,
                error=str(error),
                status_code=422,
                submitted=None if isinstance(error, TargetMatchReviewChanged) else submitted,
            )
        _flash(request, "Target-specific values confirmed for this Recipe run.")
        if (
            confirmed.status is RecipeApplicationStatus.READY
            and context.preparation_jobs is not None
        ):
            try:
                await run_in_threadpool(
                    start_next_preparation,
                    context,
                    migration_run_id,
                )
            except WorkspaceError as error:
                _flash(
                    request,
                    f"Target values are confirmed. Review and load shows the next action: {error}",
                )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}",
            status_code=303,
        )

    @router.get("/projects/{project_id}/runs/{migration_run_id}/status")
    async def integrated_run_status(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Poll the registry and job snapshots without opening child stores."""

        require_session(request)
        project = context.migration_projects.get(project_id, actor=context.actor)
        run = context.migration_runs.get(migration_run_id, actor=context.actor)
        if run.project_id != project.project_id or run.target_binding_id is None:
            return JSONResponse({"detail": "MigrationRun not found"}, status_code=404)
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        issues = context.run_planning.repository.list_run_issues(migration_run_id)
        review = build_integrated_run_review(
            context,
            bundle,
            recipes={},
            issues=issues,
        )
        return JSONResponse(
            {
                "active": review.active,
                "completed_count": review.completed_count,
                "total_count": review.total_count,
                "view_hash": review.view_hash,
                "applications": [
                    {
                        "application_id": card.application.application_id,
                        "progress_percent": card.progress_percent,
                        "progress_message": card.progress_message,
                        "message": card.message,
                    }
                    for card in review.cards
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/projects/{project_id}/runs/{migration_run_id}/prepare-next")
    async def prepare_next_recipe(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Start only the next dependency-safe Recipe preparation."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.migration_projects.get(project_id, actor=context.actor)
        run = context.migration_runs.get(migration_run_id, actor=context.actor)
        if run.project_id != project.project_id:
            return HTMLResponse("MigrationRun not found", status_code=404)
        try:
            job = await run_in_threadpool(
                start_next_preparation,
                context,
                migration_run_id,
            )
        except WorkspaceError as error:
            _flash(request, str(error))
        else:
            _flash(
                request,
                f"Preparation started: {job.message}",
            )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}",
            status_code=303,
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
        if run.purpose is MigrationRunPurpose.PRODUCTION:
            activation = await run_in_threadpool(
                context.production_runs.activation_operation, migration_run_id, actor=context.actor,
            )
            if activation is None or activation.state.value != "COMMITTED":
                return RedirectResponse(
                    f"/projects/{project_id}/production-runs/{migration_run_id}/activate", status_code=303,
                )
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
        first_unverified = next_unverified_application(bundle)
        if (
            first_unverified is not None
            and application.status.value not in {"RECONCILED", "QUALIFIED"}
            and application.application_id != first_unverified.application_id
        ):
            _flash(
                request,
                "Finish and verify the earlier Recipe before continuing this one.",
            )
            return RedirectResponse(
                f"/projects/{project_id}/runs/{migration_run_id}",
                status_code=303,
            )
        try:
            await run_in_threadpool(recover_run_preparation, context, migration_run_id)
            application = context.run_planning.repository.get_application(application_id)
        except WorkspaceError as error:
            _flash(request, str(error))
        preparation = (
            context.preparation_jobs.latest_many((application.workspace_id,)).get(
                application.workspace_id
            )
            if context.preparation_jobs is not None
            else None
        )
        load = (
            context.load_jobs.latest_many((application.workspace_id,)).get(
                application.workspace_id
            )
            if context.load_jobs is not None
            else None
        )
        step = application_resume_step(application, preparation, load)
        if step is ApplicationResumeStep.LOAD_PROGRESS:
            suffix = f"load/progress/{load.job_id}"
        elif step is ApplicationResumeStep.PREPARATION_PROGRESS:
            suffix = f"preparation/{preparation.job_id}"
        else:
            suffix = {
                ApplicationResumeStep.LOAD_RESULT: "load/outcome",
                ApplicationResumeStep.LOAD_REVIEW: "load/review",
                ApplicationResumeStep.RESOLVE: "resolution",
                ApplicationResumeStep.PREPARED_REVIEW: "normalization",
                ApplicationResumeStep.PREPARE: "prepare",
            }[step]
        destination = f"/workspaces/{application.workspace_id}/{suffix}"
        return RedirectResponse(
            destination,
            status_code=303,
        )

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/odoo",
        response_class=HTMLResponse,
    )
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
        if run.purpose not in {MigrationRunPurpose.TEST, MigrationRunPurpose.PRODUCTION}:
            return HTMLResponse("Recipe run not found", status_code=404)
        binding = context.run_setups.get(migration_run_id, actor=context.actor)
        if run.purpose is MigrationRunPurpose.PRODUCTION:
            operation = context.production_runs.activation_operation(migration_run_id, actor=context.actor)
            if operation is not None:
                return RedirectResponse(
                    f"/projects/{project_id}/production-runs/{migration_run_id}/activate", status_code=303,
                )
        data_version = context.data_versions.get(binding.data_version_id, actor=context.actor)
        _, values = context.run_setups.fresh_data_details(binding, actor=context.actor)
        if data_version.state.value != "FROZEN" or not values.ready_to_continue:
            return RedirectResponse(
                f"/projects/{project_id}/{'test-runs' if run.purpose is MigrationRunPurpose.TEST else 'production-runs'}/{migration_run_id}/fresh-data",
                status_code=303,
            )
        setup_workspace = context.migration_workspaces.get(binding.setup_workspace_id, actor=context.actor)
        setup_state = context.workspace_states.repository.get(
            setup_workspace.workspace_id
        )
        if (
            setup_state.odoo_connection_mode is None
            or not setup_state.odoo_base_url
            or not setup_state.odoo_database
        ):
            return RedirectResponse(
                f"/workspaces/{setup_workspace.workspace_id}/target",
                status_code=303,
            )
        return _render_schema(
            request,
            context,
            setup_workspace.workspace_id,
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
    export_as_of: str | None = None,
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
        export_as_of=(
            export_as_of
            if export_as_of is not None
            else datetime.now(UTC).astimezone().date().isoformat()
        ),
        error=error,
        status_code=status_code,
    )


def _run_default_value_label(field) -> str:
    """Render one target-bound default without exposing extra evidence."""

    value = field.create_default_value
    if field.type == "selection":
        label = next(
            (
                str(choice_label)
                for code, choice_label in field.selection
                if str(code) == str(value)
            ),
            str(value),
        )
        return f"{label} ({value})"
    if field.type == "boolean":
        return "Yes" if value else "No"
    if field.type == "many2one":
        return f"Odoo record #{value}"
    return str(value)


def _run_application(
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    application_id: str,
):
    """Return one application only inside the requested Project run."""

    application = context.run_planning.repository.get_application(
        application_id
    )
    if (
        application.project_id != project_id
        or application.migration_run_id != migration_run_id
    ):
        raise HTTPException(status_code=404, detail="RecipeApplication not found")
    return application


def _render_application_target_matches(
    request: Request,
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    application_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    submitted: dict[str, str] | None = None,
):
    """Render the compact, target-bound Recipe value review."""

    application = _run_application(
        context,
        project_id,
        migration_run_id,
        application_id,
    )
    review = None
    try:
        review = context.recipe_target_matches.build_review(
            application, actor=context.actor, submitted=submitted,
        )
    except WorkspaceError as review_error:
        error = error or str(review_error)
        status_code = 422
    recipe = context.recipes.get(application.recipe_id, actor=context.actor)
    workspace_state = context.queries.get(application.workspace_id)
    return _render(
        request,
        "project_recipe_target_matches.html",
        application=application,
        project_id=project_id,
        recipe=recipe,
        review=review,
        workspace_state=workspace_state,
        error=error,
        status_code=status_code,
    )
