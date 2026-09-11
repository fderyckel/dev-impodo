"""Expose Stage C model discovery, schema capture, and key governance.

Layer: web route. The router selects a configured local or remote closed
reader, obtains target-bound snapshots, and delegates their validation to
``SchemaWorkspaceService``. It also routes the exact permitted-model scope
through ``WorkspaceStateService``. No generic Odoo method or write is exposed.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_target_workflow.py``.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...application.odoo_read_failures import (
    OdooReadCredentialMissingError,
    classify_odoo_read_failure,
)
from ...application.run.recipe_run_jobs import (
    RecipeRunJob,
    RecipeRunJobKind,
    RecipeRunJobNotFoundError,
    RecipeRunJobPhase,
    RecipeRunJobProgress,
    RecipeRunJobResult,
    RecipeRunJobStatus,
)
from impodo.domain.odoo.contracts import ConnectorError
from ...domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from impodo.adapters.odoo.local_stack import LocalStackError
from impodo.domain.project.foundation import MigrationFoundationError
from ...domain.recipe.models import RecipeError
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaOrigin
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateError,
)
from ..context import WebContext
from ..run_urls import RunSetupKind
from ..forms import (
    _checked,
    _revision,
    _secure_form,
    _submitted_model_scope,
    _text,
)
from ..presenters.common import _flash, _render
from ..presenters.mapping_forms import _business_key_id, _comma_values
from ..presenters.schema import _manual_schema_models, _render_schema
from ..presenters.summary import _require_local_stack_access
from ..run_commands import start_next_preparation
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)
from impodo.web.composition.target_readers import (
    _capture_recipe_supporting_values,
    _missing_schema_reader_message,
    _refresh_model_catalog,
    _selected_local_profile,
)


async def _capture_selected_schema(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> OdooSchemaCatalog:
    """Load and persist field details for the saved Odoo model choices."""

    return await run_in_threadpool(
        _capture_selected_schema_sync,
        context,
        workspace_state,
    )


def _capture_selected_schema_sync(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> OdooSchemaCatalog:
    """Synchronous core used by both request and background-job boundaries."""

    snapshot, read_credential_binding_hash, read_identity, local_profile = (
        _read_selected_schema_sync(context, workspace_state)
    )
    schema = context.schema_workspace.capture(
        workspace_state.workspace_id,
        snapshot,
        read_credential_binding_hash=read_credential_binding_hash,
        read_identity=read_identity,
        actor=context.actor,
    )
    _mark_local_metadata_ready(context, workspace_state, schema, local_profile)
    return schema


async def _check_selected_schema(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> OdooSchemaCatalog:
    """Check saved field details without replacing current schema meaning."""

    return await run_in_threadpool(
        _check_selected_schema_sync,
        context,
        workspace_state,
    )


def _check_selected_schema_sync(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> OdooSchemaCatalog:
    """Synchronous core used by both request and background-job boundaries."""

    snapshot, read_credential_binding_hash, read_identity, local_profile = (
        _read_selected_schema_sync(context, workspace_state)
    )
    schema = context.schema_workspace.check_refresh(
        workspace_state.workspace_id,
        snapshot,
        read_credential_binding_hash=read_credential_binding_hash,
        read_identity=read_identity,
        actor=context.actor,
    )
    _mark_local_metadata_ready(context, workspace_state, schema, local_profile)
    return schema


def _read_selected_schema_sync(
    context: WebContext,
    workspace_state: WorkspaceState,
):
    """Perform the closed target read without depending on an event loop."""

    local_profile = _selected_local_profile(context, workspace_state)
    credential = get_target_credential(
        context.secret_store,
        workspace_state,
        TargetCredentialRole.READ,
    )
    if local_profile is not None and credential is None:
        snapshot = context.local_odoo_reader.get_model_metadata(
            workspace_state,
            local_profile,
            workspace_state.intended_models,
        )
        read_credential_binding_hash = local_read_credential_binding_hash(
            workspace_state
        )
        read_identity = None
    else:
        if credential is None:
            raise OdooReadCredentialMissingError(
                _missing_schema_reader_message(workspace_state)
            )
        read_identity = context.read_identity_probe(
            workspace_state,
            credential.secret,
            tuple(sorted(workspace_state.intended_models)),
        )
        snapshot = context.schema_reader(
            workspace_state,
            credential.secret,
        )
        read_credential_binding_hash = credential.binding_hash
    return (
        snapshot,
        read_credential_binding_hash,
        read_identity,
        local_profile if local_profile is not None and credential is None else None,
    )


def _mark_local_metadata_ready(
    context: WebContext,
    workspace_state: WorkspaceState,
    schema: OdooSchemaCatalog,
    local_profile,
) -> None:
    """Retain the session-only local readiness state after a successful read."""

    if local_profile is not None:
        catalog = context.queries.get_odoo_model_catalog(workspace_state.workspace_id)
        context.local_stack.mark_metadata_ready(
            workspace_state.workspace_id,
            database=schema.database,
            odoo_version=schema.odoo_version,
            model_count=(
                len(catalog.models)
                if catalog is not None
                else len(schema.models)
            ),
        )


def _odoo_source_capture_location(
    context: WebContext,
    workspace_state: WorkspaceState,
    schema: OdooSchemaCatalog,
) -> str:
    """Return the first unfinished Odoo-source capture destination."""

    selections = context.queries.get_current_odoo_capture_selections(
        workspace_state.workspace_id
    )
    planned_models = {item.model for item in selections}
    required_models = {item.name for item in schema.models}
    anchor = (
        "selection-saved"
        if required_models and planned_models == required_models
        else "capture-plan"
    )
    return f"/workspaces/{workspace_state.workspace_id}/sources#{anchor}"


def build_schema_router(context: WebContext) -> APIRouter:
    """Build the read-only schema discovery and governance routes."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/schema", response_class=HTMLResponse)
    async def workspace_schema(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        test_setup = context.run_setups.setup_binding_for_workspace(
            workspace_id,
            actor=context.actor,
        )
        if (
            workspace_state.odoo_connection_mode is None
            or not workspace_state.odoo_base_url
            or not workspace_state.odoo_database
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/target",
                status_code=303,
            )
        if test_setup is not None:
            return RedirectResponse(
                f"/projects/{test_setup.project_id}/runs/"
                f"{test_setup.migration_run_id}/odoo",
                status_code=303,
            )
        return _render_schema(request, context, workspace_id)

    @router.post(
        "/projects/{project_id}/{run_kind}/{migration_run_id}/odoo/check"
    )
    async def check_run_odoo(
        request: Request,
        project_id: str,
        migration_run_id: str,
        run_kind: RunSetupKind,
    ):
        """Check Recipe-owned Odoo requirements, then continue the owning run."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_workspace_revision", "operation_id"},
        )
        if context.recipe_run_jobs is not None:
            binding = None
            try:
                binding, _run, workspace_state, _plan = _run_odoo_check_scope(
                    context,
                    project_id,
                    migration_run_id,
                    run_kind,
                )
                if (
                    run_kind is RunSetupKind.PRODUCTION
                    and context.production_runs.activation_operation(
                        migration_run_id,
                        actor=context.actor,
                    )
                    is not None
                ):
                    return RedirectResponse(
                        f"/projects/{project_id}/production-runs/"
                        f"{migration_run_id}/activate",
                        status_code=303,
                    )
                _require_run_read_credential(context, workspace_state)
                expected_workspace_revision = int(
                    _text(form, "expected_workspace_revision")
                )
                operation_id = _text(form, "operation_id")
                job = context.recipe_run_jobs.enqueue(
                    kind=RecipeRunJobKind.ODOO_CHECK,
                    project_id=project_id,
                    migration_run_id=migration_run_id,
                    workspace_id=binding.setup_workspace_id,
                    run_purpose=run_kind.purpose,
                    work=lambda report: _perform_run_odoo_check(
                        context,
                        project_id,
                        migration_run_id,
                        run_kind,
                        expected_workspace_revision=expected_workspace_revision,
                        operation_id=operation_id,
                        report=report,
                    ),
                )
            except (
                ConnectorError,
                MigrationFoundationError,
                RecipeError,
                SecretStoreError,
                WorkspaceStateError,
                WorkspaceError,
                TypeError,
                ValueError,
            ) as error:
                if binding is None:
                    raise
                read_failure = classify_odoo_read_failure(error)
                return _render_schema(
                    request,
                    context,
                    binding.setup_workspace_id,
                    error=str(error),
                    operation_id=_text(form, "operation_id"),
                    read_credential_required=(
                        read_failure.asks_for_read_credential
                    ),
                    read_credential_resume="submit",
                    read_credential_resume_action=(
                        f"/projects/{project_id}/{run_kind}/"
                        f"{migration_run_id}/odoo/check"
                    ),
                    status_code=422,
                )
            _flash(
                request,
                "Odoo check started. You can follow each step here.",
            )
            return RedirectResponse(
                _recipe_run_progress_url(project_id, migration_run_id, job.job_id),
                status_code=303,
            )
        workspace_id = ""
        try:
            binding = context.run_setups.get(
                migration_run_id,
                actor=context.actor,
            )
            run = context.migration_runs.get(migration_run_id, actor=context.actor)
            if binding.project_id != project_id or run.purpose.value != run_kind.purpose:
                raise HTTPException(
                    status_code=404,
                    detail="Recipe run not found",
                )
            data_version = context.data_versions.get(
                binding.data_version_id,
                actor=context.actor,
            )
            if data_version.state.value != "FROZEN":
                raise MigrationFoundationError(
                    "Accept the fresh data before checking this Odoo target"
                )
            workspace_id = binding.setup_workspace_id
            _, values = context.run_setups.fresh_data_details(binding, actor=context.actor)
            if not values.ready_to_continue:
                raise MigrationFoundationError("Confirm the Recipe details on Fresh data before checking Odoo")
            if run_kind is RunSetupKind.PRODUCTION:
                operation = context.production_runs.activation_operation(migration_run_id, actor=context.actor)
                if operation is not None:
                    return RedirectResponse(f"/projects/{project_id}/production-runs/{migration_run_id}/activate", status_code=303)
            resumed = await run_in_threadpool(
                context.test_runs.resume_activation_if_needed,
                migration_run_id, actor=context.actor,
            ) if run_kind is RunSetupKind.TEST else None
            if resumed is not None:
                _flash(request, "The saved Test activation is complete. Continue with Review and load.")
                return RedirectResponse(
                    f"/projects/{project_id}/runs/{migration_run_id}", status_code=303,
                )
            workspace_state = context.queries.get(workspace_id)
            plan = context.run_setups.odoo_check_requirements_for_workspace(
                workspace_id,
                actor=context.actor,
            )
            if plan is None or not plan.models:
                raise RecipeError(
                    "The selected Recipes do not contain an Odoo requirement"
                )
            if workspace_state.intended_models != plan.model_names:
                workspace_state = context.workspace_states.update_schema_scope(
                    workspace_id,
                    actor=context.actor,
                    expected_revision=workspace_state.revision,
                    permitted_models=plan.model_names,
                )
            current = context.queries.get_odoo_schema_catalog(workspace_id)
            if current is not None and current.pending_refresh is not None:
                raise WorkspaceError(
                    "Review the detected Odoo changes before continuing"
                )
            schema = (
                await _check_selected_schema(context, workspace_state)
                if current is not None and current.origin is SchemaOrigin.LIVE_API
                else await _capture_selected_schema(context, workspace_state)
            )
            if schema.pending_refresh is not None:
                _flash(
                    request,
                    "Odoo changed. Review the changes before Impodo creates the Recipe work areas.",
                )
                return RedirectResponse(
                    f"/projects/{project_id}/runs/{migration_run_id}/odoo#odoo-schema-changes",
                    status_code=303,
                )
            await run_in_threadpool(
                _capture_recipe_supporting_values,
                context,
                workspace_state,
                schema,
                plan.supporting_values,
            )
            target_schema, target_references = (
                context.run_planning.target_evidence_from_workspace(
                    project_id,
                    workspace_id,
                    actor=context.actor,
                )
            )
            if run_kind is RunSetupKind.PRODUCTION:
                _flash(request, "Production Odoo fields and supporting values are checked. Review readiness and write access next.")
                return RedirectResponse(f"/projects/{project_id}/production-runs/{migration_run_id}/activate", status_code=303)
            if binding.state.value == "ACTIVE":
                recovered = await run_in_threadpool(
                    context.run_planning.recover_blocked_test_run_defaults,
                    migration_run_id,
                    current_schema=target_schema,
                    actor=context.actor,
                )
                if recovered:
                    _flash(
                        request,
                        (
                            "Odoo defaults are ready for review in "
                            f"{len(recovered)} Recipe"
                            f"{'s' if len(recovered) != 1 else ''}."
                        ),
                    )
                return RedirectResponse(
                    f"/projects/{project_id}/runs/{migration_run_id}",
                    status_code=303,
                )
            read_credential = get_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.READ,
            )
            if read_credential is None:
                raise OdooReadCredentialMissingError(
                    "Enter and verify the read-only Odoo key for this Test run first"
                )
            result = await run_in_threadpool(
                context.test_runs.activate,
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
            ConnectorError,
            MigrationFoundationError,
            RecipeError,
            SecretStoreError,
            WorkspaceStateError,
            WorkspaceError,
            TypeError,
            ValueError,
        ) as error:
            if not workspace_id:
                raise
            read_failure = classify_odoo_read_failure(error)
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                operation_id=_text(form, "operation_id"),
                read_credential_required=read_failure.asks_for_read_credential,
                read_credential_resume="submit",
                read_credential_resume_action=(
                    f"/projects/{project_id}/{run_kind}/"
                    f"{migration_run_id}/odoo/check"
                ),
                status_code=422,
            )
        _flash(
            request,
            f"Odoo is ready. Impodo created {len(result.applications)} Recipe work areas.",
        )
        mapped_applications = tuple(
            application
            for application in result.applications
            if getattr(application, "mapping_id", None) is not None
        )
        application_issues = (
            context.run_planning.repository.list_run_issues(migration_run_id)
            if mapped_applications
            else {}
        )
        target_value_application = next(
            (
                application
                for application in mapped_applications
                if any(
                    issue.code == "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE"
                    for issue in application_issues.get(
                        application.application_id,
                        (),
                    )
                )
            ),
            None,
        )
        if target_value_application is not None:
            _flash(
                request,
                "Odoo is ready. Confirm the target-specific values before preparation.",
            )
            return RedirectResponse(
                f"/projects/{project_id}/runs/{migration_run_id}/applications/"
                f"{target_value_application.application_id}/target-matches",
                status_code=303,
            )
        if context.preparation_jobs is not None:
            try:
                await run_in_threadpool(
                    start_next_preparation,
                    context,
                    migration_run_id,
                )
            except WorkspaceError as error:
                _flash(
                    request,
                    "Odoo is ready. Review and load shows the next action: "
                    f"{error}",
                )
        return RedirectResponse(
            f"/projects/{project_id}/runs/{migration_run_id}",
            status_code=303,
        )

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/progress/{job_id}",
        response_class=HTMLResponse,
    )
    async def recipe_run_progress(
        request: Request,
        project_id: str,
        migration_run_id: str,
        job_id: str,
    ):
        """Show resumable progress for one read-only Recipe run task."""

        require_session(request)
        job = _get_recipe_run_job(
            context,
            project_id,
            migration_run_id,
            job_id,
        )
        workspace_state = context.queries.get(job.workspace_id)
        return _render(
            request,
            "project_recipe_run_progress.html",
            workspace_state=workspace_state,
            job=job,
            progress_stages=_recipe_run_progress_stages(job),
        )

    @router.get(
        "/projects/{project_id}/runs/{migration_run_id}/progress/{job_id}/status"
    )
    async def recipe_run_progress_status(
        request: Request,
        project_id: str,
        migration_run_id: str,
        job_id: str,
    ):
        """Return the latest non-sensitive job checkpoint."""

        require_session(request)
        return JSONResponse(
            _recipe_run_job_payload(
                _get_recipe_run_job(
                    context,
                    project_id,
                    migration_run_id,
                    job_id,
                )
            )
        )

    @router.post("/workspaces/{workspace_id}/schema/local-config")
    async def select_schema_local_config(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        workspace_state = context.queries.get(workspace_id)
        try:
            _require_local_stack_access(context, workspace_state)
            selected = await run_in_threadpool(context.local_stack.pick_config)
            if selected is None:
                _flash(request, "No local Odoo setup was selected.")
            else:
                await run_in_threadpool(
                    context.local_stack.select_config,
                    workspace_id,
                    selected,
                )
                profile = _selected_local_profile(context, workspace_state)
                if profile is None:
                    raise LocalStackError(
                        "The selected odoo.conf could not be validated."
                    )
                _flash(
                    request,
                    "Selected the local Odoo setup for read-only access.",
                )
        except (LocalStackError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/models/refresh")
    async def refresh_workspace_models(request: Request, workspace_id: str):
        """Refresh persistent model choices from the exact configured target."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        workspace_state = context.queries.get(workspace_id)
        try:
            catalog = await _refresh_model_catalog(context, workspace_state)
        except (
            ConnectorError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            read_failure = classify_odoo_read_failure(error)
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                read_credential_required=read_failure.asks_for_read_credential,
                read_credential_resume="submit",
                read_credential_resume_action=(
                    f"/workspaces/{workspace_id}/schema/models/refresh"
                ),
                status_code=422,
            )
        _flash(
            request,
            f"Loaded {len(catalog.models)} available record type(s) from Odoo.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema",
            status_code=303,
        )

    @router.post(
        "/workspaces/{workspace_id}/schema",
        name="update_workspace_schema_scope",
    )
    async def update_workspace_schema_scope(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "permitted_models"})
        workspace_state = context.queries.get(workspace_id)
        test_setup = context.run_setups.setup_binding_for_workspace(
            workspace_id,
            actor=context.actor,
        )
        if test_setup is not None:
            _flash(
                request,
                "The selected Recipes already define the Odoo information for this Test run.",
            )
            return RedirectResponse(
                f"/projects/{test_setup.project_id}/runs/"
                f"{test_setup.migration_run_id}/odoo",
                status_code=303,
            )
        existing_schema = context.queries.get_odoo_schema_catalog(workspace_id)
        try:
            permitted_models = _submitted_model_scope(form)
            model_catalog = context.queries.get_odoo_model_catalog(workspace_id)
            if model_catalog:
                available = {model.name for model in model_catalog.models}
                unknown = [
                    model for model in permitted_models if model not in available
                ]
                if unknown:
                    raise WorkspaceStateError(
                        f"{unknown[0]} is not in the refreshed Odoo model catalogue"
                    )
            saved_workspace_state = context.workspace_states.update_schema_scope(
                workspace_id,
                actor=context.actor,
                expected_revision=_revision(form),
                permitted_models=permitted_models,
            )
        except WorkspaceStateError as error:
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        needs_capture = (
            saved_workspace_state.revision != workspace_state.revision
            or existing_schema is None
            or existing_schema.origin is not SchemaOrigin.LIVE_API
        )
        if needs_capture:
            try:
                await _capture_selected_schema(context, saved_workspace_state)
            except (
                ConnectorError,
                WorkspaceStateError,
                SecretStoreError,
                WorkspaceError,
            ) as error:
                read_failure = classify_odoo_read_failure(error)
                return _render_schema(
                    request,
                    context,
                    workspace_id,
                    support_error=str(error),
                    schema_load_failed=True,
                    read_credential_required=(
                        read_failure.asks_for_read_credential
                    ),
                    read_credential_resume="submit",
                    read_credential_resume_action=(
                        f"/workspaces/{workspace_id}/schema/capture"
                    ),
                    status_code=422,
                )
        _flash(request, "Odoo data is ready.")
        if saved_workspace_state.source_mode is SourceMode.ODOO:
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            if schema is not None:
                return RedirectResponse(
                    _odoo_source_capture_location(
                        context,
                        saved_workspace_state,
                        schema,
                    ),
                    status_code=303,
                )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#odoo-details",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/capture")
    async def capture_workspace_schema(request: Request, workspace_id: str):
        """Capture first metadata or compare a refresh with current evidence."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "return_to_sources"},
        )
        return_to_sources = _checked(form, "return_to_sources")
        workspace_state = context.queries.get(workspace_id)
        test_setup = context.run_setups.setup_binding_for_workspace(
            workspace_id,
            actor=context.actor,
        )
        if test_setup is not None:
            return RedirectResponse(
                f"/projects/{test_setup.project_id}/runs/"
                f"{test_setup.migration_run_id}/odoo",
                status_code=303,
            )
        current = context.queries.get_odoo_schema_catalog(workspace_id)
        try:
            schema = (
                await _check_selected_schema(context, workspace_state)
                if current is not None and current.origin is SchemaOrigin.LIVE_API
                else await _capture_selected_schema(context, workspace_state)
            )
        except (
            ConnectorError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            read_failure = classify_odoo_read_failure(error)
            return _render_schema(
                request,
                context,
                workspace_id,
                support_error=str(error),
                schema_load_failed=True,
                read_credential_required=read_failure.asks_for_read_credential,
                read_credential_resume="submit",
                read_credential_resume_action=(
                    f"/workspaces/{workspace_id}/schema/capture"
                ),
                status_code=422,
            )
        if schema.pending_refresh is not None:
            _flash(
                request,
                "Odoo changes need review. Your current work was preserved.",
            )
        elif current is not None and current.origin is SchemaOrigin.LIVE_API:
            _flash(
                request,
                "Odoo details are unchanged. Your current work remains available.",
            )
        else:
            _flash(request, "Odoo data is ready.")
        if (
            return_to_sources
            and schema.pending_refresh is None
            and workspace_state.source_mode is SourceMode.ODOO
        ):
            return RedirectResponse(
                _odoo_source_capture_location(context, workspace_state, schema),
                status_code=303,
            )
        if (
            schema.pending_refresh is None
            and not (
                current is not None
                and current.origin is SchemaOrigin.LIVE_API
            )
            and workspace_state.source_mode is SourceMode.ODOO
        ):
            return RedirectResponse(
                _odoo_source_capture_location(context, workspace_state, schema),
                status_code=303,
            )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#odoo-details",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/capture/confirm")
    async def confirm_workspace_schema_refresh(request: Request, workspace_id: str):
        """Promote one reviewed checked snapshot and invalidate dependents."""

        form = await request.form()
        workspace_state = context.queries.get(workspace_id)
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "expected_current_content_hash",
                "candidate_id",
                "candidate_semantic_hash",
                "confirm_schema_refresh",
            },
        )
        if not _checked(form, "confirm_schema_refresh"):
            return _render_schema(
                request,
                context,
                workspace_id,
                error=(
                    "Confirm that Impodo may replace the saved Odoo details "
                    "and retire work that depends on them"
                ),
                status_code=422,
            )
        try:
            schema = context.schema_workspace.confirm_refresh(
                workspace_id,
                expected_current_content_hash=_text(
                    form, "expected_current_content_hash"
                ),
                expected_candidate_id=_text(form, "candidate_id"),
                expected_candidate_semantic_hash=_text(
                    form, "candidate_semantic_hash"
                ),
                actor=context.actor,
            )
        except (WorkspaceStateError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Updated the saved Odoo details. Review the stages that now need attention.",
        )
        if workspace_state.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                _odoo_source_capture_location(context, workspace_state, schema),
                status_code=303,
            )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#odoo-details",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/local-draft")
    async def create_local_schema_draft(request: Request, workspace_id: str):
        form = await request.form()
        workspace_state = context.queries.get(workspace_id)
        allowed = {"csrf_token", "acknowledge_local_draft"} | {
            name
            for index, _model in enumerate(workspace_state.intended_models)
            for name in (
                f"manual_model_label_{index}",
                f"manual_fields_{index}",
            )
        }
        _secure_form(request, form, allowed)
        try:
            if not _checked(form, "acknowledge_local_draft"):
                raise WorkspaceError(
                    "Acknowledge that this local schema is unverified"
                )
            schema = context.schema_workspace.capture_local_manual(
                workspace_id,
                _manual_schema_models(workspace_state, form),
                read_credential_binding_hash=(
                    local_read_credential_binding_hash(workspace_state)
                ),
                actor=context.actor,
            )
        except (WorkspaceStateError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Created an unchecked local draft for "
                f"{len(schema.models)} Odoo choice(s)."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/govern")
    async def govern_workspace_schema(request: Request, workspace_id: str):
        """Confirm business-key definitions against the current schema."""

        form = await request.form()
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        if schema is None:
            raise HTTPException(status_code=422, detail="Odoo schema missing")
        allowed = {"csrf_token"} | {
            name
            for index, _model in enumerate(schema.models)
            for name in (
                f"key_fields_{index}",
                f"scope_fields_{index}",
                f"key_description_{index}",
                f"primary_key_field_{index}",
                f"primary_scope_field_{index}",
            )
        }
        _secure_form(request, form, allowed)
        key_drafts: dict[
            str,
            tuple[tuple[str, ...], tuple[str, ...], str],
        ] = {}
        for index, model in enumerate(schema.models):
            primary_key = _text(form, f"primary_key_field_{index}")
            key_fields = (
                (primary_key,)
                if primary_key
                else _comma_values(_text(form, f"key_fields_{index}"))
            )
            primary_scope = _text(form, f"primary_scope_field_{index}")
            scope_fields = (
                (primary_scope,)
                if primary_scope
                else _comma_values(_text(form, f"scope_fields_{index}"))
            )
            key_drafts[model.name] = (
                key_fields,
                scope_fields,
                _text(form, f"key_description_{index}"),
            )

        definitions: list[BusinessKeyDefinition] = []
        key_errors: dict[str, str] = {}
        for model in schema.models:
            key_fields, scope_fields, description = key_drafts[model.name]
            if not key_fields:
                continue
            try:
                definitions.append(
                    BusinessKeyDefinition(
                        key_id=_business_key_id(
                            model.name, key_fields, scope_fields
                        ),
                        model=model.name,
                        key_fields=key_fields,
                        scope_fields=scope_fields,
                        description=description,
                        status=BusinessKeyStatus.CONFIRMED,
                    )
                )
            except ValueError as error:
                if str(error) == "Business-key fields and scope must be unique":
                    key_errors[model.name] = (
                        f"For {model.label}, choose each field only once. "
                        "The matching fields and Within fields must be different."
                    )
                else:
                    key_errors[model.name] = (
                        f"Review the matching rule for {model.label}: {error}"
                    )
        if key_errors:
            return _render_schema(
                request,
                context,
                workspace_id,
                error=(
                    "Review the highlighted matching rule, then confirm it again."
                ),
                status_code=422,
                key_drafts=key_drafts,
                key_errors=key_errors,
            )
        try:
            governance = context.schema_workspace.govern(
                workspace_id,
                business_keys=definitions,
                actor=context.actor,
            )
        except (ValueError, WorkspaceError) as error:
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
                key_drafts=key_drafts,
            )
        _flash(
            request,
            (
                f"Confirmed {len(governance.business_keys)} Odoo matching rule(s)."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/mapping",
            status_code=303,
        )

    return router


def _run_odoo_check_scope(
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    run_kind: RunSetupKind,
):
    """Validate the immutable run scope before starting or repeating a job."""

    binding = context.run_setups.get(
        migration_run_id,
        actor=context.actor,
    )
    run = context.migration_runs.get(migration_run_id, actor=context.actor)
    if binding.project_id != project_id or run.purpose.value != run_kind.purpose:
        raise HTTPException(status_code=404, detail="Recipe run not found")
    data_version = context.data_versions.get(
        binding.data_version_id,
        actor=context.actor,
    )
    if data_version.state.value != "FROZEN":
        raise MigrationFoundationError(
            "Accept the fresh data before checking this Odoo target"
        )
    _, values = context.run_setups.fresh_data_details(
        binding,
        actor=context.actor,
    )
    if not values.ready_to_continue:
        raise MigrationFoundationError(
            "Confirm the Recipe details on Fresh data before checking Odoo"
        )
    workspace_state = context.queries.get(binding.setup_workspace_id)
    plan = context.run_setups.odoo_check_requirements_for_workspace(
        workspace_state.workspace_id,
        actor=context.actor,
    )
    if plan is None or not plan.models:
        raise RecipeError(
            "The selected Recipes do not contain an Odoo requirement"
        )
    return binding, run, workspace_state, plan


def _require_run_read_credential(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> None:
    """Keep the existing credential prompt ahead of background work."""

    local_profile = _selected_local_profile(context, workspace_state)
    credential = get_target_credential(
        context.secret_store,
        workspace_state,
        TargetCredentialRole.READ,
    )
    if local_profile is None and credential is None:
        raise OdooReadCredentialMissingError(
            _missing_schema_reader_message(workspace_state)
        )


def _perform_run_odoo_check(
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    run_kind: RunSetupKind,
    *,
    expected_workspace_revision: int,
    operation_id: str,
    report: Callable[[RecipeRunJobProgress], None],
) -> RecipeRunJobResult:
    """Execute the former request-bound Odoo check with visible checkpoints."""

    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.VALIDATING,
            "Checking the saved run setup",
            5,
        )
    )
    binding, _run, workspace_state, plan = _run_odoo_check_scope(
        context,
        project_id,
        migration_run_id,
        run_kind,
    )
    if run_kind is RunSetupKind.PRODUCTION:
        operation = context.production_runs.activation_operation(
            migration_run_id,
            actor=context.actor,
        )
        if operation is not None:
            return RecipeRunJobResult(
                f"/projects/{project_id}/production-runs/"
                f"{migration_run_id}/activate",
                "Odoo check is ready for Production review",
            )
    for application in context.run_planning.repository.list_applications(
        migration_run_id
    ):
        context.recipe_target_matches.clear_prepared_review(
            application.application_id
        )
    if run_kind is RunSetupKind.TEST:
        resumed = context.test_runs.resume_activation_if_needed(
            migration_run_id,
            actor=context.actor,
            progress=lambda completed, total, recipe_id: (
                _report_materialization(
                    report,
                    completed,
                    total,
                    recipe_id,
                )
            ),
        )
        if resumed is not None:
            return _complete_test_run_check(
                context,
                project_id,
                migration_run_id,
                resumed,
                report,
                start_preparation=False,
            )
    if workspace_state.intended_models != plan.model_names:
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            permitted_models=plan.model_names,
        )
    current = context.queries.get_odoo_schema_catalog(
        workspace_state.workspace_id
    )
    if current is not None and current.pending_refresh is not None:
        raise WorkspaceError(
            "Review the detected Odoo changes before continuing"
        )
    field_count = sum(len(item.field_names) for item in plan.models)
    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.READING_ODOO,
            "Connecting to this Odoo and reading the required fields",
            10,
            total_units=field_count,
            unit_label="required fields",
        )
    )
    schema = (
        _check_selected_schema_sync(context, workspace_state)
        if current is not None and current.origin is SchemaOrigin.LIVE_API
        else _capture_selected_schema_sync(context, workspace_state)
    )
    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.CHECKING_FIELDS,
            f"Checked {field_count} required Odoo field"
            f"{'s' if field_count != 1 else ''}",
            30,
            completed_units=field_count,
            total_units=field_count,
            unit_label="required fields",
        )
    )
    if schema.pending_refresh is not None:
        return RecipeRunJobResult(
            f"/projects/{project_id}/runs/{migration_run_id}"
            "/odoo#odoo-schema-changes",
            "Odoo changes are ready for review",
        )
    supporting_count = len(plan.supporting_values)
    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.SUPPORTING_VALUES,
            (
                "Reading current supporting values from Odoo"
                if supporting_count
                else "No supporting value sets are needed"
            ),
            35,
            total_units=supporting_count,
            unit_label="supporting value sets",
        )
    )
    _capture_recipe_supporting_values(
        context,
        workspace_state,
        schema,
        plan.supporting_values,
    )
    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.SUPPORTING_VALUES,
            (
                f"Read {supporting_count} supporting value set"
                f"{'s' if supporting_count != 1 else ''}"
                if supporting_count
                else "No supporting value sets were needed"
            ),
            50,
            completed_units=supporting_count,
            total_units=supporting_count,
            unit_label="supporting value sets",
        )
    )
    target_schema, target_references = (
        context.run_planning.target_evidence_from_workspace(
            project_id,
            workspace_state.workspace_id,
            actor=context.actor,
        )
    )
    if run_kind is RunSetupKind.PRODUCTION:
        return RecipeRunJobResult(
            f"/projects/{project_id}/production-runs/"
            f"{migration_run_id}/activate",
            "Odoo fields and supporting values are ready",
        )
    if binding.state.value == "ACTIVE":
        report(
            RecipeRunJobProgress(
                RecipeRunJobPhase.MATERIALIZING,
                "Updating the Recipe work areas with checked Odoo defaults",
                60,
            )
        )
        context.run_planning.recover_blocked_test_run_defaults(
            migration_run_id,
            current_schema=target_schema,
            actor=context.actor,
        )
        bundle = context.run_planning.repository.get_bundle(migration_run_id)
        return _complete_test_run_check(
            context,
            project_id,
            migration_run_id,
            bundle,
            report,
            start_preparation=False,
        )
    read_credential = get_target_credential(
        context.secret_store,
        workspace_state,
        TargetCredentialRole.READ,
    )
    if read_credential is None:
        raise OdooReadCredentialMissingError(
            "Enter and verify the read-only Odoo key for this Test run first"
        )
    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.MATERIALIZING,
            "Creating the Recipe work areas",
            55,
        )
    )
    result = context.test_runs.activate(
        project_id,
        migration_run_id,
        expected_workspace_revision=expected_workspace_revision,
        target_schema=target_schema,
        target_reference_bundle=target_references,
        credential_generation=read_credential.binding_hash,
        operation_id=operation_id,
        actor=context.actor,
        progress=lambda completed, total, recipe_id: _report_materialization(
            report,
            completed,
            total,
            recipe_id,
        ),
    )
    return _complete_test_run_check(
        context,
        project_id,
        migration_run_id,
        result,
        report,
        start_preparation=True,
    )


def _report_materialization(
    report: Callable[[RecipeRunJobProgress], None],
    completed: int,
    total: int,
    _recipe_id: str,
) -> None:
    total = max(0, int(total))
    completed = min(total, max(0, int(completed)))
    fraction = completed / total if total else 0.0
    report(
        RecipeRunJobProgress(
            RecipeRunJobPhase.MATERIALIZING,
            (
                f"Created {completed} of {total} Recipe work areas"
                if total
                else "Creating the Recipe work areas"
            ),
            55 + round(30 * fraction),
            completed_units=completed,
            total_units=total,
            unit_label="Recipe work areas",
        )
    )


def _complete_test_run_check(
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    result,
    report: Callable[[RecipeRunJobProgress], None],
    *,
    start_preparation: bool,
) -> RecipeRunJobResult:
    """Prepare focused reviews so their browser route is a bounded read."""

    mapped_applications = tuple(
        application
        for application in result.applications
        if getattr(application, "mapping_id", None) is not None
    )
    application_issues = (
        context.run_planning.repository.list_run_issues(migration_run_id)
        if mapped_applications
        else {}
    )
    target_applications = tuple(
        application
        for application in mapped_applications
        if any(
            issue.code == "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE"
            for issue in application_issues.get(
                application.application_id,
                (),
            )
        )
    )
    target_count = len(target_applications)
    if not target_count:
        report(
            RecipeRunJobProgress(
                RecipeRunJobPhase.TARGET_MATCHES,
                "All target-specific values already match",
                99,
            )
        )
    for index, application in enumerate(target_applications):
        report(
            RecipeRunJobProgress(
                RecipeRunJobPhase.TARGET_MATCHES,
                f"Preparing target values for Recipe {index + 1} of "
                f"{target_count}",
                88 + round(11 * index / target_count),
                completed_units=index,
                total_units=target_count,
                unit_label="Recipe reviews",
            )
        )
        context.recipe_target_matches.prepare_review(
            application,
            actor=context.actor,
        )
        report(
            RecipeRunJobProgress(
                RecipeRunJobPhase.TARGET_MATCHES,
                f"Prepared {index + 1} of {target_count} Recipe reviews",
                88 + round(11 * (index + 1) / target_count),
                completed_units=index + 1,
                total_units=target_count,
                unit_label="Recipe reviews",
            )
        )
    if target_applications:
        first = target_applications[0]
        return RecipeRunJobResult(
            f"/projects/{project_id}/runs/{migration_run_id}/applications/"
            f"{first.application_id}/target-matches",
            "Target-specific values are ready for review",
        )
    if start_preparation and context.preparation_jobs is not None:
        try:
            start_next_preparation(context, migration_run_id)
        except WorkspaceError:
            pass
    return RecipeRunJobResult(
        f"/projects/{project_id}/runs/{migration_run_id}",
        "Odoo is ready for Review and load",
    )


def _get_recipe_run_job(
    context: WebContext,
    project_id: str,
    migration_run_id: str,
    job_id: str,
) -> RecipeRunJob:
    if context.recipe_run_jobs is None:
        raise HTTPException(status_code=404, detail="Recipe run job not found")
    project = context.migration_projects.get(project_id, actor=context.actor)
    run = context.migration_runs.get(migration_run_id, actor=context.actor)
    if run.project_id != project.project_id:
        raise HTTPException(status_code=404, detail="Recipe run job not found")
    try:
        return context.recipe_run_jobs.get(
            project_id,
            migration_run_id,
            job_id,
        )
    except RecipeRunJobNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail="Recipe run job not found",
        ) from error


def _recipe_run_job_payload(job: RecipeRunJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "kind": job.kind.value,
        "run_purpose": job.run_purpose,
        "status": job.status.value,
        "phase": job.phase.value,
        "message": job.message,
        "progress_percent": job.progress_percent,
        "completed_units": job.completed_units,
        "total_units": job.total_units,
        "unit_label": job.unit_label,
        "updated_at": job.updated_at.isoformat(),
        "failure_message": job.failure_message,
        "redirect_url": job.redirect_url,
        "stages": _recipe_run_progress_stages(job),
    }


def _recipe_run_progress_url(
    project_id: str,
    migration_run_id: str,
    job_id: str,
) -> str:
    return (
        f"/projects/{project_id}/runs/{migration_run_id}/progress/{job_id}"
    )


def _recipe_run_progress_stages(job: RecipeRunJob) -> tuple[dict[str, str], ...]:
    if job.kind is RecipeRunJobKind.TARGET_MATCH_REVIEW:
        definitions = (
            (RecipeRunJobPhase.VALIDATING, "Check the Recipe application"),
            (RecipeRunJobPhase.TARGET_MATCHES, "Prepare target values"),
        )
    elif job.run_purpose == "PRODUCTION":
        definitions = (
            (RecipeRunJobPhase.VALIDATING, "Check saved run setup"),
            (RecipeRunJobPhase.READING_ODOO, "Connect to this Odoo"),
            (RecipeRunJobPhase.SUPPORTING_VALUES, "Read supporting values"),
        )
    else:
        definitions = (
            (RecipeRunJobPhase.VALIDATING, "Check saved run setup"),
            (RecipeRunJobPhase.READING_ODOO, "Connect to this Odoo"),
            (RecipeRunJobPhase.SUPPORTING_VALUES, "Read supporting values"),
            (RecipeRunJobPhase.MATERIALIZING, "Prepare Recipe work areas"),
            (RecipeRunJobPhase.TARGET_MATCHES, "Prepare target values"),
        )
    phase_index = {
        RecipeRunJobPhase.QUEUED: -1,
        RecipeRunJobPhase.VALIDATING: 0,
        RecipeRunJobPhase.READING_ODOO: 1,
        RecipeRunJobPhase.CHECKING_FIELDS: 1,
        RecipeRunJobPhase.SUPPORTING_VALUES: 2,
        RecipeRunJobPhase.MATERIALIZING: 3,
        RecipeRunJobPhase.TARGET_MATCHES: 4,
        RecipeRunJobPhase.COMPLETE: len(definitions),
    }
    if job.kind is RecipeRunJobKind.TARGET_MATCH_REVIEW:
        phase_index.update(
            {
                RecipeRunJobPhase.TARGET_MATCHES: 1,
                RecipeRunJobPhase.COMPLETE: 2,
            }
        )
    elif job.run_purpose == "PRODUCTION":
        phase_index.update(
            {
                RecipeRunJobPhase.COMPLETE: 3,
            }
        )
    active_index = phase_index[job.phase]
    stages = []
    for index, (phase, label) in enumerate(definitions):
        if job.status is RecipeRunJobStatus.FAILED and index == active_index:
            state = "attention"
            status_label = "Needs attention"
        elif index < active_index or job.status is RecipeRunJobStatus.SUCCEEDED:
            state = "complete"
            status_label = "Complete"
        elif index == active_index:
            state = "current"
            status_label = "Current"
        else:
            state = "waiting"
            status_label = "Waiting"
        stages.append(
            {
                "phase": phase.value,
                "label": label,
                "state": state,
                "status_label": status_label,
            }
        )
    return tuple(stages)
