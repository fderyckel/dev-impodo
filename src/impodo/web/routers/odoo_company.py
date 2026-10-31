"""Confirm one run's target company while retaining saved source and mappings.

See docs/developer/workflow/02-odoo-data.md.
"""

from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceStateError,
    transfer_destination_workspace,
)
from impodo.web.composition.odoo_company import read_company_access
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.composition.target_readers import _selected_local_profile
from impodo.web.context import WebContext
from impodo.web.forms import _revision, _secure_form, _text
from impodo.web.presenters.common import _flash
from impodo.web.presenters.summary import _render_target
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session
from impodo.web.target_credentials import TargetCredentialRole, get_target_credential

from .transfer_destination import _render_destination


def build_odoo_company_router(context: WebContext) -> APIRouter:
    """Authorize local preparation, release its scope, then read company choices."""
    router = APIRouter()

    def prepare(request: Request, workspace_id: str):
        actor = request_identity(request, context.identity_resolver).actor
        context.workspace_states.authorization.require(
            actor, Capability.PROJECT_EDIT, workspace_id=workspace_id
        )
        state = context.queries.get(workspace_id)
        if state.odoo_connection_mode is None:
            raise WorkspaceStateError(
                "Choose a company for the configured file-import target"
            )
        target = (
            transfer_destination_workspace(state)
            if state.source_mode is SourceMode.ODOO
            else state
        )
        if (
            target.odoo_connection_mode is OdooConnectionMode.LOCAL
            and state.source_mode is SourceMode.FILE
        ):
            profile = _selected_local_profile(context, target)
            if profile is None:
                raise WorkspaceStateError(
                    "Find local Odoo and validate its configuration before choosing a company"
                )
            return actor, target, None, profile
        owner = (
            state
            if state.source_mode is SourceMode.ODOO
            else context.target_credential_workspace(
                workspace_id, workspace_state=state, actor=actor
            )
        )
        credential = get_target_credential(
            context.secret_store,
            owner,
            TargetCredentialRole.DESTINATION_TRANSFER
            if state.source_mode is SourceMode.ODOO
            else TargetCredentialRole.READ,
        )
        if credential is None:
            raise SecretStoreError(
                "Enter the checking key before choosing an Odoo company"
            )
        return actor, target, credential, None

    async def read_access(state, credential, profile):
        if profile is not None:
            return await run_in_threadpool(
                context.local_odoo_reader.get_company_access,
                replace(state, odoo_company=None),
                profile,
            )
        if credential is None:
            raise SecretStoreError(
                "Enter the checking key before choosing an Odoo company"
            )
        return await run_in_threadpool(read_company_access, state, credential.secret)

    def render(request, workspace_id, **kwargs):
        state = context.queries.get(workspace_id)
        renderer = (
            _render_destination
            if state.source_mode is SourceMode.ODOO
            else _render_target
        )
        return renderer(request, context, state, **kwargs)

    @router.post("/workspaces/{workspace_id}/target/company/choices")
    @router.post("/workspaces/{workspace_id}/transfer-destination/company/choices")
    async def choices(request: Request, workspace_id: str):
        require_session(request)
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            _actor, state, credential, profile = await run_local_operation(
                prepare, request, workspace_id
            )
            access = await read_access(state, credential, profile)
        except (WorkspaceStateError, SecretStoreError, ConnectorError) as error:
            return await run_page_read(
                render, request, workspace_id, error=str(error), status_code=422
            )
        return await run_page_read(render, request, workspace_id, company_access=access)

    @router.post("/workspaces/{workspace_id}/target/company")
    @router.post("/workspaces/{workspace_id}/transfer-destination/company")
    async def select(request: Request, workspace_id: str):
        require_session(request)
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision", "company_id"})
        try:
            _actor, state, credential, profile = await run_local_operation(
                prepare, request, workspace_id
            )
            revision = _revision(form)
            if state.revision != revision:
                raise WorkspaceStateError(
                    "The workspace changed; reload before choosing its company"
                )
            access = await read_access(state, credential, profile)
            selection = next(
                (
                    item
                    for item in access.choices
                    if str(item.company_id) == _text(form, "company_id")
                ),
                None,
            )
            if selection is None:
                raise WorkspaceStateError("Choose a company available to this API user")

            def save():
                actor, current, current_credential, current_profile = prepare(
                    request, workspace_id
                )
                if current_profile != profile or (
                    current_credential.binding_hash if current_credential else None
                ) != (credential.binding_hash if credential else None):
                    raise WorkspaceStateError(
                        "The checking key changed; check company choices again"
                    )
                return context.workspace_states.select_odoo_company(
                    current.workspace_id,
                    actor=actor,
                    expected_revision=revision,
                    selection=selection,
                )

            await run_local_operation(save)
        except (
            WorkspaceStateError,
            SecretStoreError,
            ConnectorError,
            ValueError,
        ) as error:
            return await run_page_read(
                render, request, workspace_id, error=str(error), status_code=422
            )
        _flash(
            request,
            "Odoo company saved. Check Odoo details and saved matches, then prepare and compare again. Your source data and mapping rules are kept.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/{'transfer-destination' if state.source_mode is SourceMode.ODOO else 'target'}#odoo-operation-company",
            status_code=303,
        )

    return router
