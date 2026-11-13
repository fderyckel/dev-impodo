"""Select authenticated source reads for reusable Odoo data captures.

Local admission and credential storage finish before any Odoo request. Capture
work uses the selected source company and never borrows destination access.
"""

from __future__ import annotations

from impodo.application.odoo_saved_data_service import SavedOdooCaptureAccess
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_database_binding import OdooDatabaseBindingRole
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.shared.tenancy import AuthorizationReceipt, RequestOrganizationScope
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState
from impodo.web.context import WebContext
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)

from .database_bindings import frozen_database_guard, review_database_binding
from .page_reads import run_local_operation_sync
from .target_readers import _company_choice_connector


def saved_source_companies(
    context: WebContext, workspace_id: str, *, actor: Actor
) -> tuple[OdooCompanySelection, ...]:
    """Fetch real source company choices before accepting capture scope."""

    def prepare():
        context.workspace_access.require(
            actor, Capability.SOURCE_CONFIGURE, workspace_id=workspace_id
        )
        state = context.queries.get(workspace_id)
        context.odoo_database_bindings.require_current(
            workspace_id, OdooDatabaseBindingRole.SOURCE,
            base_url=state.odoo_base_url, database=state.odoo_database,
            actor=actor, capability=Capability.SOURCE_CONFIGURE,
        )
        credential = get_target_credential(
            context.secret_store, state, TargetCredentialRole.READ
        )
        if credential is None:
            raise WorkspaceError(
                "Enter the source checking key before choosing a company"
            )
        return state, credential

    state, credential = run_local_operation_sync(prepare)
    return (
        _company_choice_connector(state, credential.secret).get_company_access().choices
    )


def saved_capture_access(
    context: WebContext,
    fresh: WorkspaceState,
    source_workspace_id: str,
    *,
    actor: Actor,
    scope: RequestOrganizationScope,
    authorization_receipt: AuthorizationReceipt,
) -> SavedOdooCaptureAccess:
    """Read fresh schema under the chosen source company outside local scopes."""

    def prepare():
        context.workspace_access.require(
            actor, Capability.SOURCE_CAPTURE, workspace_id=source_workspace_id
        )
        context.workspace_access.require(
            actor, Capability.SOURCE_CAPTURE, workspace_id=fresh.workspace_id
        )
        source = context.queries.get(source_workspace_id)
        original_binding = context.odoo_database_bindings.require_current(
            source_workspace_id, OdooDatabaseBindingRole.SOURCE,
            base_url=source.odoo_base_url, database=source.odoo_database,
            actor=actor, capability=Capability.SOURCE_CAPTURE,
        )
        credential = get_target_credential(
            context.secret_store, source, TargetCredentialRole.READ
        )
        if credential is None:
            raise WorkspaceError(
                "Enter the source checking key again before updating saved data"
            )
        new_credential = store_target_credential(
            context.secret_store,
            fresh,
            TargetCredentialRole.READ,
            credential.secret,
            persistent=credential.persistent,
        )
        return credential.secret, new_credential.binding_hash, original_binding

    key, binding, original_binding = run_local_operation_sync(prepare)
    admitted = review_database_binding(
        context, fresh, OdooDatabaseBindingRole.SOURCE,
        actor=actor, scope=scope, authorization_receipt=authorization_receipt,
    )
    if admitted.generation_hash != original_binding.generation_hash:
        raise WorkspaceError("The source database changed during review. Start a fresh source review.")
    guard = frozen_database_guard(
        context, fresh, actor=actor, role=OdooDatabaseBindingRole.SOURCE,
        capability=Capability.SOURCE_CAPTURE,
    )
    guard()
    model_identity = context.read_identity_probe(fresh, key, ("ir.model",))
    guard()
    model_catalog = context.model_catalog_reader(fresh, key)
    guard()
    identity = context.read_identity_probe(
        fresh, key, tuple(sorted(fresh.intended_models))
    )
    guard()
    metadata = context.schema_reader(fresh, key)
    guard()
    return SavedOdooCaptureAccess(
        metadata,
        identity,
        binding,
        context.source_capture_factory(fresh, key),
        model_catalog=model_catalog,
        model_catalog_identity=model_identity,
    )
