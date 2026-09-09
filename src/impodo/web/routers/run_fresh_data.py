"""Expose one Recipe-aware Fresh data flow for Test and Production."""

from dataclasses import replace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.data_version.inspection import SourceInspectionError
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceRegistrationError, WorkspaceStateError, WorkspaceStatus
from ..context import WebContext
from ..forms import _revision, _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..source_file_commands import accept_source_uploads, remove_source_file
from ..run_urls import RunSetupKind, fresh_data_url as _fresh_data_url


def build_run_fresh_data_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data",
        response_class=HTMLResponse,
    )
    async def run_fresh_data(
        request: Request,
        project_id: str,
        migration_run_id: str,
        run_kind: RunSetupKind,
    ):
        """Show the exact Recipe-owned source needs for this delivery."""

        require_session(request)
        return await run_in_threadpool(
                _render_fresh_data,
            request,
            context,
            project_id,
            migration_run_id,
            run_kind=run_kind,
        )

    @router.post(
        "/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data/files"
    )
    async def add_run_fresh_files(
        request: Request,
        project_id: str,
        migration_run_id: str,
        run_kind: RunSetupKind,
    ):
        """Add fresh files to the run-owned setup workspace."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "source_file"})
        try:
            view = await run_in_threadpool(_fresh_data_view, context, project_id, migration_run_id, run_kind=run_kind)
            _require_editable_fresh_files(view)
            added_files = await accept_source_uploads(
                context,
                view["setup_workspace"].workspace_id,
                form,
            )
        except WorkspaceStateError as error:
            return await run_in_threadpool(
                _render_fresh_data,
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                run_kind=run_kind,
            )
        _flash(
            request,
            f"Added {len(added_files)} fresh "
            f"file{'s' if len(added_files) != 1 else ''} to this run.",
        )
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id, run_kind=run_kind),
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data/"
        "files/{file_id}/remove"
    )
    async def remove_run_fresh_file(
        request: Request,
        project_id: str,
        migration_run_id: str,
        run_kind: RunSetupKind,
        file_id: str,
    ):
        """Remove one fresh file before the run starts table review."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            view = await run_in_threadpool(_fresh_data_view, context, project_id, migration_run_id, run_kind=run_kind)
            _require_editable_fresh_files(view)
            removed = await remove_source_file(
                context,
                view["setup_workspace"].workspace_id,
                file_id,
                expected_revision=_revision(form),
            )
        except WorkspaceStateError as error:
            return await run_in_threadpool(
                _render_fresh_data,
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                run_kind=run_kind,
            )
        _flash(request, f"Removed {removed.display_name} from this run.")
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id, run_kind=run_kind),
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data/register"
    )
    async def register_run_fresh_files(
        request: Request,
        project_id: str,
        migration_run_id: str,
        run_kind: RunSetupKind,
    ):
        """Inspect fresh files and return their Recipe-owned table matches."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            expected_revision = _revision(form)
            view = await run_in_threadpool(_fresh_data_view, context, project_id, migration_run_id, run_kind=run_kind)
            _require_editable_fresh_files(view)
            workspace = view["setup_state"]
            if workspace.status is WorkspaceStatus.DRAFT:
                workspace = await run_in_threadpool(
                    context.workspace_states.register,
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
            return await run_in_threadpool(
                _render_fresh_data,
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                problems=error.problems,
                status_code=422,
                run_kind=run_kind,
            )
        except (SourceInspectionError, WorkspaceStateError) as error:
            return await run_in_threadpool(
                _render_fresh_data,
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                run_kind=run_kind,
            )
        _flash(
            request,
            f"Checked {len(catalogs)} fresh "
            f"file{'s' if len(catalogs) != 1 else ''} and matched their tables "
            "to the Recipe inputs.",
        )
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id, run_kind=run_kind),
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data/accept"
    )
    async def accept_run_fresh_data(
        request: Request,
        project_id: str,
        migration_run_id: str,
        run_kind: RunSetupKind,
    ):
        """Accept the exact current logical-to-physical table matches."""

        form = await request.form()
        initial_view = await run_in_threadpool(
            _fresh_data_view,
            context,
            project_id,
            migration_run_id,
            run_kind=run_kind,
        )
        initial_plan = initial_view["match_plan"]
        run_value_plan = initial_view["run_value_plan"]
        editable_run_values = run_value_plan.editable_values
        editable_controls = run_value_plan.editable_controls
        allowed = {
            "csrf_token",
            "parameter_revision",
            "warnings_acknowledged",
            "intent",
        }
        if initial_plan is not None:
            allowed.update(
                f"match_{index}" for index, _item in enumerate(initial_plan.inputs)
            )
        allowed.update(
            f"parameter_{index}"
            for index, _item in enumerate(editable_run_values)
        )
        allowed.update(f"control_{index}" for index, _item in enumerate(editable_controls))
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
        submitted_controls: dict[str, dict[str, str]] = {}
        for index, item in enumerate(editable_controls):
            submitted_controls.setdefault(item.recipe_id, {})[
                item.requirement.logical_control_id
            ] = _text(form, f"control_{index}")
        expected_parameter_revision = None
        try:
            expected_parameter_revision = _optional_parameter_revision(form)
            if _text(form, "intent") == "save":
                await run_in_threadpool(context.run_setups.replace_values, initial_view["binding"], submitted_run_values,
                    supplied_controls=submitted_controls, expected_revision=expected_parameter_revision, actor=context.actor)
                _flash(request, "Saved the details for this run. Review its table matches before accepting the delivery.")
                return RedirectResponse(_fresh_data_url(project_id, migration_run_id, run_kind=run_kind), status_code=303)
            view = dict(initial_view)
            if initial_plan is not None and overrides:
                view["match_plan"] = context.run_setups.match_plan(
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
            if run_value_plan.requires_confirmation:
                await run_in_threadpool(
                    context.run_setups.replace_values,
                    view["binding"],
                    submitted_run_values,
                    supplied_controls=submitted_controls,
                    expected_revision=expected_parameter_revision,
                    actor=context.actor,
                )
            if view["accepted"]:
                _flash(request, "Saved the details for this run.")
                return RedirectResponse(
                    _fresh_data_url(project_id, migration_run_id, run_kind=run_kind),
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
        except (MigrationFoundationError, WorkspaceError, ValueError) as error:
            return await run_in_threadpool(
                _render_fresh_data,
                request,
                context,
                project_id,
                migration_run_id,
                error=str(error),
                status_code=422,
                match_overrides=overrides,
                parameter_overrides=submitted_run_values,
                control_overrides=submitted_controls,
                run_kind=run_kind,
            )
        _flash(
            request,
            "Accepted the fresh data and its Recipe table matches.",
        )
        return RedirectResponse(
            _fresh_data_url(project_id, migration_run_id, run_kind=run_kind),
            status_code=303,
        )

    return router


def _render_fresh_data(
    request: Request,
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    *,
    run_kind: RunSetupKind,
    error: str | None = None,
    problems: tuple[str, ...] = (),
    status_code: int = 200,
    match_overrides: dict[str, str] | None = None,
    parameter_overrides: dict[str, str] | None = None,
    control_overrides: dict[str, dict[str, str]] | None = None,
):
    view = _fresh_data_view(
        context,
        project_id,
        migration_run_id,
        match_overrides=match_overrides,
        parameter_overrides=parameter_overrides,
        control_overrides=control_overrides,
        run_kind=run_kind,
    )
    return _render(
        request,
        "project_run_fresh_data.html",
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


def _fresh_data_view(
    context,
    project_id,
    migration_run_id,
    *,
    run_kind: RunSetupKind,
    match_overrides=None,
    parameter_overrides=None,
    control_overrides=None,
):
    project = context.migration_projects.get(project_id, actor=context.actor)
    binding = context.run_setups.get(migration_run_id, actor=context.actor)
    if binding.project_id != project.project_id:
        raise HTTPException(status_code=404, detail="Recipe run not found")
    run = context.migration_runs.get(migration_run_id, actor=context.actor)
    if run.purpose.value != run_kind.purpose:
        raise HTTPException(status_code=404, detail="Recipe run not found")
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
    requirements, run_value_plan = context.run_setups.fresh_data_details(binding, actor=context.actor)
    if parameter_overrides is not None or control_overrides is not None:
        run_value_plan = replace(
            run_value_plan,
            values=tuple(
                replace(
                    item,
                    supplied_value=(parameter_overrides or {}).get(
                        item.logical_parameter_id,
                        item.supplied_value,
                    ),
                )
                for item in run_value_plan.values
            ),
            controls=tuple(
                replace(item, supplied_value=(control_overrides or {}).get(item.recipe_id, {}).get(
                    item.requirement.logical_control_id, item.supplied_value,
                ))
                for item in run_value_plan.controls
            ),
            confirmed=False,
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
        context.run_setups.match_plan(
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
            f"/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data/accept"
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
        "run_kind": run_kind,
        "run_label": "Test" if run_kind is RunSetupKind.TEST else "Production",
        "fresh_data_url": _fresh_data_url(project_id, migration_run_id, run_kind=run_kind),
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
