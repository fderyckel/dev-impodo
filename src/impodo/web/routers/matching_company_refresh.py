"""Refresh company-sensitive Stage 3 values between two bounded local operations.

See docs/developer/workflow/03-match-data.md.
"""

from dataclasses import dataclass, field, replace

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.adapters.odoo.local_stack import LocalStackProfile
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import OdooModelCatalog, OdooSchemaCatalog
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.composition.target_readers import _selected_local_profile
from impodo.web.context import WebContext
from impodo.web.forms import _revision, _secure_form
from impodo.web.presenters.common import _flash
from impodo.web.presenters.mapping_view import _render_mapping
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session
from impodo.web.target_credentials import (
    TargetCredential,
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)


@dataclass(frozen=True, slots=True)
class PreparedMatchingAccess:
    """Hold metadata and one role-bound reader after the local scope closes."""

    workspace: WorkspaceState
    schema: OdooSchemaCatalog
    credential: TargetCredential | None = field(repr=False)
    profile: LocalStackProfile | None
    needs_refresh: bool
    force: bool
    model_catalog: OdooModelCatalog | None = None


def prepare_matching_access(
    context: WebContext,
    workspace_id: str,
    *,
    actor: Actor,
    schema: OdooSchemaCatalog | None = None,
    expected_revision: int | None = None,
    force: bool = False,
) -> PreparedMatchingAccess:
    """Read once within the caller's bounded local operation; never contact Odoo."""
    context.workspace_states.authorization.require(
        actor,
        Capability.MAPPING_EDIT,
        workspace_id=workspace_id,
    )
    state = context.queries.get(workspace_id)
    catalog = (
        schema
        if schema is not None
        else context.queries.get_odoo_schema_catalog(workspace_id)
    )
    if (
        catalog is None
        or expected_revision is not None
        and state.revision != expected_revision
    ):
        raise WorkspaceError(
            "Matching inputs changed; reload Match data before checking Odoo access"
        )
    if force and (
        state.source_mode is not SourceMode.FILE or state.odoo_company is None
    ):
        raise WorkspaceError(
            "Choose the company for this load in Match data before checking company-specific values"
        )
    if (
        state.source_mode is not SourceMode.FILE
        or catalog.pending_refresh is not None
        and not force
    ):
        return PreparedMatchingAccess(state, catalog, None, None, False, force)
    profile = _selected_local_profile(context, state)
    owner = context.target_credential_workspace(
        workspace_id, workspace_state=state, actor=actor
    )
    credential = get_target_credential(
        context.secret_store, owner, TargetCredentialRole.READ
    )
    if profile is None and credential is None:
        if force:
            raise SecretStoreError(
                "Enter the checking key before checking company-specific values"
            )
        # Keep the existing missing-key/recovery handling at the reader boundary.
        return PreparedMatchingAccess(state, catalog, None, None, False, force)
    stale = (
        credential is not None
        and credential.binding_hash != catalog.read_credential_binding_hash
        or state.odoo_company is not None
        and state.odoo_company.context_hash != catalog.read_context_hash
    )
    return PreparedMatchingAccess(
        state,
        catalog,
        credential,
        profile,
        force or stale,
        force,
        context.queries.get_odoo_model_catalog(workspace_id)
        if force or stale
        else None,
    )


def _access_binding(catalog: OdooSchemaCatalog):
    return (
        catalog.content_hash,
        catalog.read_credential_binding_hash,
        catalog.read_context_hash,
        catalog.pending_refresh.candidate_id
        if catalog.pending_refresh is not None
        else None,
    )


async def check_matching_access(
    context: WebContext,
    prepared: PreparedMatchingAccess,
    *,
    actor: Actor,
) -> OdooSchemaCatalog:
    """Refresh stale access once, with no local database owner across the RPC."""
    if not prepared.needs_refresh:
        return prepared.schema
    state, profile, credential = (
        prepared.workspace,
        prepared.profile,
        prepared.credential,
    )
    models = tuple(sorted(model.name for model in prepared.schema.models))

    def read():
        session = None
        if profile is not None and credential is None:
            snapshot = context.local_odoo_reader.get_model_metadata(
                state, profile, models
            )
            identity = None
            binding = local_read_credential_binding_hash(state)
        else:
            if credential is None:
                raise SecretStoreError(
                    "Enter the checking key before checking company-specific values"
                )
            binding = credential.binding_hash
            if context.schema_read_session_factory is not None:
                session = context.schema_read_session_factory(state, credential.secret)
                snapshot = session.read_models(models)
                identity = session.verify_identity(models)
            else:
                snapshot = context.schema_reader(
                    replace(state, intended_models=models), credential.secret
                )
                identity = context.read_identity_probe(state, credential.secret, models)
        model_snapshot = None
        model_identity = None
        if prepared.model_catalog is not None:
            if profile is not None and credential is None:
                model_snapshot = context.local_odoo_reader.get_model_catalog(
                    state, profile
                )
            elif session is not None:
                model_snapshot = session.read_model_catalog()
                model_identity = session.verify_identity(("ir.model",))
            elif credential is not None:
                model_snapshot = context.model_catalog_reader(state, credential.secret)
                model_identity = context.read_identity_probe(
                    state, credential.secret, ("ir.model",)
                )
            if (identity is None) != (model_identity is None) or (
                identity is not None
                and model_identity is not None
                and (
                    identity.target_hash != model_identity.target_hash
                    or identity.principal_hash != model_identity.principal_hash
                    or identity.context_hash != model_identity.context_hash
                )
            ):
                raise WorkspaceError(
                    "The Odoo reader or company access changed while checking model choices and fields. Check matches again."
                )
        return snapshot, identity, binding, model_snapshot, model_identity

    (
        snapshot,
        identity,
        binding,
        model_snapshot,
        model_identity,
    ) = await run_in_threadpool(read)

    def publish():
        current = prepare_matching_access(
            context,
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            force=prepared.force,
        )
        if (
            _access_binding(current.schema) != _access_binding(prepared.schema)
            or current.model_catalog != prepared.model_catalog
            or current.profile != profile
            or (current.credential.binding_hash if current.credential else None)
            != (credential.binding_hash if credential else None)
        ):
            raise WorkspaceError(
                "Odoo access or matching inputs changed; check matches again"
            )
        if model_snapshot is not None:
            context.schema_workspace.discover_models(
                state.workspace_id,
                model_snapshot,
                read_credential_binding_hash=binding,
                read_identity=model_identity,
                actor=actor,
            )
        return context.schema_workspace.check_refresh(
            state.workspace_id,
            snapshot,
            read_credential_binding_hash=binding,
            read_identity=identity,
            actor=actor,
        )

    return await run_local_operation(publish)


def build_matching_company_refresh_router(context: WebContext) -> APIRouter:
    """Read metadata and defaults under the selected company without losing mapping."""
    router = APIRouter()

    @router.post("/workspaces/{workspace_id}/mapping/company/refresh")
    async def refresh(request: Request, workspace_id: str):
        require_session(request)
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        actor = request_identity(request, context.identity_resolver).actor

        try:
            prepared = await run_local_operation(
                prepare_matching_access,
                context,
                workspace_id,
                actor=actor,
                expected_revision=_revision(form),
                force=True,
            )
            checked = await check_matching_access(context, prepared, actor=actor)
        except (WorkspaceError, ValueError, SecretStoreError, ConnectorError) as error:
            return await run_page_read(
                _render_mapping,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        if checked.pending_refresh:
            _flash(
                request,
                "Odoo fields or access changed. Review the detected changes; saved matches are kept.",
            )
            return RedirectResponse(
                f"/workspaces/{workspace_id}/schema#odoo-schema-changes",
                status_code=303,
            )
        _flash(
            request,
            "Company-specific values checked. Review linked-value matches and check the current field matches before preparing.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/mapping#odoo-operation-company",
            status_code=303,
        )

    return router
