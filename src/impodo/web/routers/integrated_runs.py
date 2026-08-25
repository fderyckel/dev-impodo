"""Expose Project-owned integrated Test run planning and status."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...inspection import SourceInspectionError
from ...migration_foundation import MigrationFoundationError
from ...migration_run_planning import RecipeDependency
from ...migration_runs import MigrationRunPurpose
from ...recipes import RecipeError
from ...workspace_errors import WorkspaceError
from ...workspace_state import (
    SourceMode,
    WorkspaceRegistrationError,
    WorkspaceStateError,
    WorkspaceStatus,
)
from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.schema import _render_schema
from ..run_review import build_integrated_run_review, start_next_preparation
from ..security import require_session
from ..source_file_commands import accept_source_uploads, remove_source_file


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
        return _render_test_fresh_data(
            request,
            context,
            project_id,
            migration_run_id,
        )

    @router.post(
        "/projects/{project_id}/test-runs/{migration_run_id}/fresh-data/files"
    )
    async def add_test_run_fresh_files(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Add fresh files to the run-owned setup workspace."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "source_file"})
        try:
            view = _test_fresh_data_view(context, project_id, migration_run_id)
            _require_editable_fresh_files(view)
            added_files = await accept_source_uploads(
                context,
                view["setup_workspace"].workspace_id,
                form,
            )
        except WorkspaceStateError as error:
            return _render_test_fresh_data(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Added {len(added_files)} fresh "
            f"file{'s' if len(added_files) != 1 else ''} to this Test run.",
        )
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id),
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/test-runs/{migration_run_id}/fresh-data/"
        "files/{file_id}/remove"
    )
    async def remove_test_run_fresh_file(
        request: Request,
        project_id: str,
        migration_run_id: str,
        file_id: str,
    ):
        """Remove one fresh file before the run starts table review."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            view = _test_fresh_data_view(context, project_id, migration_run_id)
            _require_editable_fresh_files(view)
            removed = await remove_source_file(
                context,
                view["setup_workspace"].workspace_id,
                file_id,
                expected_revision=_revision(form),
            )
        except WorkspaceStateError as error:
            return _render_test_fresh_data(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, f"Removed {removed.display_name} from this Test run.")
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id),
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/test-runs/{migration_run_id}/fresh-data/register"
    )
    async def register_test_run_fresh_files(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Inspect fresh files and return their Recipe-owned table matches."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            expected_revision = _revision(form)
            view = _test_fresh_data_view(context, project_id, migration_run_id)
            _require_editable_fresh_files(view)
            workspace = view["setup_state"]
            if workspace.status is WorkspaceStatus.DRAFT:
                workspace = context.workspace_states.register(
                    view["setup_workspace"].workspace_id,
                    actor=context.actor,
                    expected_revision=expected_revision,
                )
            catalogs = await run_in_threadpool(
                context.inspections.inspect_project,
                workspace.workspace_id,
                actor=context.actor,
            )
        except WorkspaceRegistrationError as error:
            return _render_test_fresh_data(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                problems=error.problems,
                status_code=422,
            )
        except (SourceInspectionError, WorkspaceStateError) as error:
            return _render_test_fresh_data(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Checked {len(catalogs)} fresh "
            f"file{'s' if len(catalogs) != 1 else ''} and matched their tables "
            "to the Recipe inputs.",
        )
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id),
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/test-runs/{migration_run_id}/fresh-data/accept"
    )
    async def accept_test_run_fresh_data(
        request: Request,
        project_id: str,
        migration_run_id: str,
    ):
        """Accept the exact current logical-to-physical table matches."""

        form = await request.form()
        initial_view = _test_fresh_data_view(
            context,
            project_id,
            migration_run_id,
        )
        initial_plan = initial_view["match_plan"]
        run_value_plan = initial_view["run_value_plan"]
        editable_run_values = run_value_plan.editable_values
        allowed = {
            "csrf_token",
            "parameter_revision",
            "warnings_acknowledged",
        }
        if initial_plan is not None:
            allowed.update(
                f"match_{index}" for index, _item in enumerate(initial_plan.inputs)
            )
        allowed.update(
            f"parameter_{index}"
            for index, _item in enumerate(editable_run_values)
        )
        _secure_form(request, form, allowed)
        overrides = (
            {
                item.logical_dataset_id: _text(form, f"match_{index}")
                for index, item in enumerate(initial_plan.inputs)
                if _text(form, f"match_{index}")
            }
            if initial_plan is not None
            else {}
        )
        submitted_run_values = {
            item.logical_parameter_id: _text(form, f"parameter_{index}")
            for index, item in enumerate(editable_run_values)
        }
        expected_parameter_revision = None
        try:
            expected_parameter_revision = _optional_parameter_revision(form)
            view = dict(initial_view)
            if initial_plan is not None and overrides:
                view["match_plan"] = context.test_runs.fresh_data_match_plan(
                    initial_view["requirements"],
                    initial_view["catalogs"],
                    overrides=overrides,
                )
            selection = view["source_selection"]
            tables_by_file: dict[str, list[str]] = {}
            dataset_names: dict[tuple[str, str], str] = {}
            warnings_acknowledged = False
            if not view["accepted"] and selection is None:
                if view["setup_state"].status is not WorkspaceStatus.REGISTERED:
                    raise WorkspaceError("Check the fresh files before accepting them")
                match_plan = view["match_plan"]
                if match_plan is None or not match_plan.ready_to_accept:
                    raise WorkspaceError(
                        "Resolve every missing, ambiguous, or unused source input "
                        "before accepting this fresh data"
                    )
                warnings_acknowledged = (
                    _text(form, "warnings_acknowledged") == "1"
                )
                if match_plan.warnings and not warnings_acknowledged:
                    raise WorkspaceError(
                        "Review and acknowledge the detected file warnings"
                    )
                selected_candidates = tuple(
                    item.selected_candidate for item in match_plan.inputs
                )
                for input_match, candidate in zip(
                    match_plan.inputs,
                    selected_candidates,
                    strict=True,
                ):
                    if candidate is None:
                        raise WorkspaceError("A Recipe source match is incomplete")
                    tables_by_file.setdefault(candidate.file_id, []).append(
                        candidate.table_key
                    )
                    dataset_names[(candidate.file_id, candidate.table_key)] = (
                        input_match.dataset_name
                    )
            if editable_run_values:
                await run_in_threadpool(
                    context.test_runs.replace_fresh_data_run_values,
                    view["binding"],
                    submitted_run_values,
                    expected_revision=expected_parameter_revision,
                    actor=context.actor,
                )
            if view["accepted"]:
                _flash(request, "Saved the details for this Test run.")
                return RedirectResponse(
                    _fresh_data_url(project_id, migration_run_id),
                    status_code=303,
                )
            if selection is None:
                for source_file in view["setup_state"].source_files:
                    await run_in_threadpool(
                        context.sources.confirm_source,
                        view["setup_workspace"].workspace_id,
                        source_file.file_id,
                        selected_table_keys=tuple(
                            tables_by_file.get(source_file.file_id, ())
                        ),
                        warnings_acknowledged=warnings_acknowledged,
                        actor=context.actor,
                    )
                selection = await run_in_threadpool(
                    context.sources.freeze_selection,
                    view["setup_workspace"].workspace_id,
                    dataset_names=dataset_names,
                    actor=context.actor,
                )
            await run_in_threadpool(
                context.data_version_source_projection.accept_file_selection,
                view["setup_workspace"].workspace_id,
                selection,
                actor=context.actor,
            )
        except (MigrationFoundationError, WorkspaceError) as error:
            return _render_test_fresh_data(
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                match_overrides=overrides,
                parameter_overrides=submitted_run_values,
            )
        _flash(
            request,
            "Accepted the fresh data and its Recipe table matches.",
        )
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id),
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
        applications = {item.recipe_id: item for item in bundle.applications}
        ordered = tuple(
            applications[recipe_id]
            for recipe_id in bundle.requirement_plan.application_order
        )
        first_unverified = next(
            (
                item
                for item in ordered
                if item.status.value not in {"RECONCILED", "QUALIFIED"}
            ),
            None,
        )
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
        if load is not None:
            if load.active:
                destination = (
                    f"/workspaces/{application.workspace_id}/load/progress/"
                    f"{load.job_id}"
                )
            elif load.status.value == "SUCCEEDED":
                destination = f"/workspaces/{application.workspace_id}/load/outcome"
            else:
                destination = f"/workspaces/{application.workspace_id}/load/review"
        elif preparation is not None:
            if preparation.active:
                destination = (
                    f"/workspaces/{application.workspace_id}/preparation/"
                    f"{preparation.job_id}"
                )
            elif preparation.status.value == "REVIEW_REQUIRED":
                destination = f"/workspaces/{application.workspace_id}/resolution"
            elif preparation.status.value == "SUCCEEDED":
                destination = f"/workspaces/{application.workspace_id}/normalization"
            else:
                destination = f"/workspaces/{application.workspace_id}/prepare"
        elif application.status.value == "EXECUTED":
            destination = f"/workspaces/{application.workspace_id}/load/outcome"
        elif application.status.value == "COMPARED":
            destination = f"/workspaces/{application.workspace_id}/load/review"
        elif application.status.value == "PREPARED":
            destination = f"/workspaces/{application.workspace_id}/normalization"
        else:
            destination = f"/workspaces/{application.workspace_id}/prepare"
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
        if run.purpose is MigrationRunPurpose.TEST:
            data_version = context.data_versions.get(
                run.data_version_id,
                actor=context.actor,
            )
            if data_version.state.value != "FROZEN":
                return RedirectResponse(
                    _fresh_data_url(project_id, migration_run_id),
                    status_code=303,
                )
            binding = context.test_runs.get(
                migration_run_id,
                actor=context.actor,
            )
            if binding.project_id != project.project_id:
                return HTMLResponse("Test run not found", status_code=404)
            setup_workspace = context.migration_workspaces.get(
                binding.setup_workspace_id,
                actor=context.actor,
            )
        elif run.purpose is MigrationRunPurpose.PRODUCTION:
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
        else:
            return HTMLResponse("Recipe run not found", status_code=404)
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


def _fresh_data_url(project_id: str, migration_run_id: str) -> str:
    return f"/projects/{project_id}/test-runs/{migration_run_id}/fresh-data"


def _render_test_fresh_data(
    request: Request,
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    *,
    error: str | None = None,
    problems: tuple[str, ...] = (),
    status_code: int = 200,
    match_overrides: dict[str, str] | None = None,
    parameter_overrides: dict[str, str] | None = None,
):
    view = _test_fresh_data_view(
        context,
        project_id,
        migration_run_id,
        match_overrides=match_overrides,
        parameter_overrides=parameter_overrides,
    )
    return _render(
        request,
        "project_test_run_fresh_data.html",
        **view,
        workspace_state=view["setup_state"],
        error=error,
        problems=problems,
        status_code=status_code,
    )


def _require_editable_fresh_files(view) -> None:
    setup_state = view["setup_state"]
    if (
        view["accepted"]
        or view["source_selection"] is not None
        or setup_state.status
        not in {WorkspaceStatus.DRAFT, WorkspaceStatus.REGISTERED}
        or setup_state.source_mode is not SourceMode.FILE
    ):
        raise WorkspaceStateError(
            "Fresh files can only be changed before this fresh data is accepted"
        )


def _test_fresh_data_view(
    context,
    project_id,
    migration_run_id,
    *,
    match_overrides=None,
    parameter_overrides=None,
):
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
    requirements = context.test_runs.fresh_data_requirements(
        migration_run_id,
        actor=context.actor,
    )
    run_value_plan = context.test_runs.fresh_data_run_value_plan(
        binding,
        requirements,
        actor=context.actor,
    )
    if parameter_overrides:
        run_value_plan = replace(
            run_value_plan,
            values=tuple(
                replace(
                    item,
                    supplied_value=parameter_overrides.get(
                        item.logical_parameter_id,
                        item.supplied_value,
                    ),
                )
                for item in run_value_plan.values
            ),
        )
    source_selection = context.queries.get_source_selection(
        binding.setup_workspace_id
    )
    catalogs = (
        context.queries.get_source_catalogs(binding.setup_workspace_id)
        if setup_state.status is WorkspaceStatus.REGISTERED
        else ()
    )
    catalog_file_ids = {item.file_id for item in catalogs}
    inspection_complete = bool(setup_state.source_files) and catalog_file_ids == {
        item.file_id for item in setup_state.source_files
    }
    match_plan = (
        context.test_runs.fresh_data_match_plan(
            requirements,
            catalogs,
            overrides=match_overrides,
        )
        if inspection_complete and source_selection is None
        else None
    )
    accepted = data_version.state.value == "FROZEN"
    fresh_data_complete = accepted and run_value_plan.ready_to_continue
    can_edit_files = (
        not accepted
        and source_selection is None
        and setup_state.status
        in {WorkspaceStatus.DRAFT, WorkspaceStatus.REGISTERED}
        and setup_state.source_mode is SourceMode.FILE
    )
    if fresh_data_complete:
        action_href = f"/projects/{project_id}/runs/{migration_run_id}/odoo"
        action_label = "Continue to Check Odoo"
    elif source_selection is not None:
        action_href = (
            f"/projects/{project_id}/test-runs/{migration_run_id}/fresh-data/accept"
        )
        action_label = "Finish accepting fresh data"
    elif can_edit_files:
        action_href = None
        action_label = None
    else:
        action_href = None
        action_label = None
    return {
        "accepted": accepted,
        "action_href": action_href,
        "action_label": action_label,
        "binding": binding,
        "catalogs": catalogs,
        "can_edit_files": can_edit_files,
        "data_version": data_version,
        "fresh_data_complete": fresh_data_complete,
        "inspection_complete": inspection_complete,
        "match_plan": match_plan,
        "project": project,
        "requirements": requirements,
        "run_value_plan": run_value_plan,
        "run": run,
        "source_selection": source_selection,
        "setup_state": setup_state,
        "setup_workspace": setup_workspace,
    }


def _optional_parameter_revision(form) -> int | None:
    value = _text(form, "parameter_revision")
    if not value:
        return None
    try:
        revision = int(value)
    except ValueError as error:
        raise WorkspaceStateError("Invalid run value revision") from error
    if revision < 1:
        raise WorkspaceStateError("Invalid run value revision")
    return revision
