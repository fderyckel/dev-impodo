"""Expose Stage C model discovery, schema capture, and key governance.

Layer: web route. The router selects a configured local or remote closed
reader, obtains target-bound snapshots, and delegates their validation to
``SchemaWorkspaceService``. It also routes the exact permitted-model scope
through ``WorkspaceStateService``. No generic Odoo method or write is exposed.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from ...connectors import ConnectorError
from ...local_stack import LocalStackError
from ...domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from ...workspace_state import WorkspaceState, WorkspaceStateError
from ...secrets import SecretStoreError
from ...workspace_contracts import OdooSchemaCatalog, SchemaOrigin
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import (
    _checked,
    _revision,
    _secure_form,
    _submitted_model_scope,
    _text,
)
from ..presenters.common import _flash
from ..presenters.mapping_forms import _business_key_id, _comma_values
from ..presenters.schema import _manual_schema_models, _render_schema
from ..presenters.summary import _require_local_stack_access
from ..target_readers import (
    _missing_schema_reader_message,
    _refresh_model_catalog,
    _selected_local_profile,
)
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)


async def _capture_selected_schema(
    context: WebContext,
    workspace_state: WorkspaceState,
) -> OdooSchemaCatalog:
    """Load and persist field details for the saved Odoo model choices."""

    snapshot, read_credential_binding_hash, read_identity, local_profile = (
        await _read_selected_schema(context, workspace_state)
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

    snapshot, read_credential_binding_hash, read_identity, local_profile = (
        await _read_selected_schema(context, workspace_state)
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


async def _read_selected_schema(context: WebContext, workspace_state: WorkspaceState):
    """Read one closed metadata snapshot and its verified access provenance."""

    local_profile = _selected_local_profile(context, workspace_state)
    credential = get_target_credential(
        context.secret_store,
        workspace_state,
        TargetCredentialRole.READ,
    )
    if local_profile is not None and credential is None:
        snapshot = await run_in_threadpool(
            context.local_odoo_reader.get_model_metadata,
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
            raise WorkspaceError(_missing_schema_reader_message(workspace_state))
        read_identity = await run_in_threadpool(
            context.read_identity_probe,
            workspace_state,
            credential.secret,
            tuple(sorted(workspace_state.intended_models)),
        )
        snapshot = await run_in_threadpool(
            context.schema_reader,
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


def build_schema_router(context: WebContext) -> APIRouter:
    """Build the read-only schema discovery and governance routes."""

    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/schema", response_class=HTMLResponse)
    async def workspace_schema(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.odoo_connection_mode is None
            or not workspace_state.odoo_base_url
            or not workspace_state.odoo_database
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_state.workspace_id}/target",
                status_code=303,
            )
        return _render_schema(request, context, workspace_id)

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
            return _render_schema(
                request,
                context,
                workspace_id,
                error=str(error),
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
            or existing_schema.origin is SchemaOrigin.LOCAL_MANUAL
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
                return _render_schema(
                    request,
                    context,
                    workspace_id,
                    support_error=str(error),
                    schema_load_failed=True,
                    status_code=422,
                )
        _flash(request, "Odoo data is ready.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#odoo-details",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/capture")
    async def capture_workspace_schema(request: Request, workspace_id: str):
        """Capture first metadata or compare a refresh with current evidence."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        workspace_state = context.queries.get(workspace_id)
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
            return _render_schema(
                request,
                context,
                workspace_id,
                support_error=str(error),
                schema_load_failed=True,
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
        return RedirectResponse(
            f"/workspaces/{workspace_id}/schema#odoo-details",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/schema/capture/confirm")
    async def confirm_workspace_schema_refresh(request: Request, workspace_id: str):
        """Promote one reviewed checked snapshot and invalidate dependents."""

        form = await request.form()
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
            context.schema_workspace.confirm_refresh(
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
