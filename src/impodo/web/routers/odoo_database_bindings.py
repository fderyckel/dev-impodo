"""Review database reset declarations and saved-source rebinding locally.

Known workspace ownership supplies every endpoint. A declaration records an
already completed reset in Impodo; it never resets or writes to Odoo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData

from impodo.application.shared.identity import ResolvedRequestIdentity
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBindingRole,
    OdooDatabaseGenerationConflict,
)
from impodo.domain.project.foundation import MigrationFoundationError, require_uuid
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.context import WebContext
from impodo.web.forms import _revision, _secure_form, _text
from impodo.web.presenters.common import _flash, _render
from impodo.web.request_identity import request_identity
from impodo.web.security import require_session

_ROLES = (OdooDatabaseBindingRole.SOURCE, OdooDatabaseBindingRole.DESTINATION)
_SAFE_ERRORS = (MigrationFoundationError, ValueError)


@dataclass(frozen=True, slots=True)
class _KnownConnection:
    """Project one server-derived physical database and its operating company."""

    workspace: WorkspaceState
    role: OdooDatabaseBindingRole
    base_url: str
    database: str
    company: str


def _role(value: str) -> OdooDatabaseBindingRole:
    try:
        return OdooDatabaseBindingRole(value)
    except ValueError as error:
        raise HTTPException(422, "Choose Source or Destination") from error


def _known_connection(
    state: WorkspaceState, role: OdooDatabaseBindingRole
) -> _KnownConnection:
    if role is OdooDatabaseBindingRole.SOURCE:
        if state.source_mode is not SourceMode.ODOO:
            raise ValueError("Choose a workspace that uses an Odoo source database")
        base_url, database, company = (
            state.odoo_base_url,
            state.odoo_database,
            state.odoo_company,
        )
    elif state.source_mode is SourceMode.ODOO:
        base_url, database, company = (
            state.destination_odoo_base_url,
            state.destination_odoo_database,
            state.destination_company,
        )
    else:
        base_url, database, company = (
            state.odoo_base_url,
            state.odoo_database,
            state.odoo_company,
        )
    if not base_url or not database:
        raise ValueError(
            "Connect and review this workspace's Odoo database before continuing"
        )
    return _KnownConnection(
        state, role, base_url, database, company.name if company else "Not selected"
    )


def _location(project_id: str, **query: str) -> str:
    suffix = urlencode({name: value for name, value in query.items() if value})
    return f"/projects/{project_id}/odoo-database-bindings" + (
        f"?{suffix}" if suffix else ""
    )


def _single_form(request: Request, form: FormData, allowed: set[str]) -> None:
    _secure_form(request, form, allowed)
    if any(len(form.getlist(name)) > 1 for name in allowed):
        raise HTTPException(422, "Repeated form fields are not accepted")


def _can_declare(identity: ResolvedRequestIdentity, project_id: str) -> bool:
    return bool(
        identity.authorization.project_visibility.organization_wide
        and identity.actor.has(Capability.PROJECT_ADMIN)
        and identity.authorization.authorizes(
            Capability.PROJECT_ADMIN, at=datetime.now(UTC), project_id=project_id
        )
    )


def _project(
    context: WebContext,
    project_id: str,
    identity: ResolvedRequestIdentity,
    capability: Capability = Capability.PROJECT_VIEW,
):
    return context.project_queries.get_visible_project(
        project_id,
        scope=identity.organization_scope,
        authorization=identity.authorization,
        capability=capability,
    )


def _owned_workspace(
    context: WebContext,
    project_id: str,
    workspace_id: str,
    identity: ResolvedRequestIdentity,
) -> WorkspaceState:
    owner = context.migration_workspaces.get(
        require_uuid(workspace_id, "workspace_id"), actor=identity.actor
    )
    if owner.project_id != project_id:
        raise HTTPException(404, "Workspace not found in this data project")
    return context.queries.get(owner.workspace_id)


def _binding_view(
    context: WebContext, connection: _KnownConnection, identity: ResolvedRequestIdentity
):
    current = context.odoo_database_bindings.current(
        connection.workspace.workspace_id,
        base_url=connection.base_url,
        database=connection.database,
        actor=identity.actor,
        capability=Capability.PROJECT_VIEW,
        scope=identity.organization_scope,
        authorization_receipt=identity.authorization,
    )
    binding = context.odoo_database_bindings.get(
        connection.workspace.workspace_id,
        connection.role,
        base_url=connection.base_url,
        database=connection.database,
        actor=identity.actor,
        capability=Capability.PROJECT_VIEW,
    )
    return {
        "connection": connection,
        "generation": current,
        "binding": binding,
        "binding_current": binding is not None
        and binding.generation_hash == current.generation_hash,
    }


def _page(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    identity: ResolvedRequestIdentity,
    workspace_id: str | None = None,
    role: OdooDatabaseBindingRole = OdooDatabaseBindingRole.SOURCE,
    saved_data_id: str | None = None,
    rebind_workspace_id: str | None = None,
    error: str | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
):
    """Project only registry labels and the explicitly selected workspace states."""

    project = _project(context, project_id, identity)
    workspaces = context.migration_workspaces.list_for_project(
        project_id, actor=identity.actor
    )
    roots = {item.workspace_id: item for item in workspaces}
    saved = context.saved_odoo_data
    definition = (
        saved.get(project_id, saved_data_id, actor=identity.actor)
        if saved is not None and saved_data_id
        else None
    )
    selected_id = workspace_id or (
        definition.source_workspace_id if definition else None
    )
    selected = None
    states: dict[str, WorkspaceState] = {}
    connection_problem = None
    if selected_id:
        if selected_id not in roots:
            raise HTTPException(404, "Workspace not found in this data project")
        states[selected_id] = context.queries.get(selected_id)
        try:
            selected = _binding_view(
                context, _known_connection(states[selected_id], role), identity
            )
        except _SAFE_ERRORS as problem:
            connection_problem = str(problem)
    replacement = None
    replacement_problem = None
    if definition is not None and rebind_workspace_id:
        if rebind_workspace_id not in roots:
            raise HTTPException(
                404, "Replacement source workspace not found in this data project"
            )
        state = states.get(rebind_workspace_id)
        if state is None:
            state = context.queries.get(rebind_workspace_id)
        try:
            replacement = _binding_view(
                context,
                _known_connection(state, OdooDatabaseBindingRole.SOURCE),
                identity,
            )
            if rebind_workspace_id == definition.source_workspace_id:
                replacement_problem = (
                    "Choose a fresh source workspace reviewed after the reset"
                )
            elif context.queries.get_source_selection(rebind_workspace_id) is not None:
                replacement_problem = "Choose a fresh source workspace before accepting or capturing its data"
            elif state.odoo_company is None:
                replacement_problem = (
                    "Choose and review the source company in the fresh workspace"
                )
            elif not replacement["binding_current"]:
                replacement_problem = "Review this fresh source connection against the current database before rebinding"
        except _SAFE_ERRORS as problem:
            replacement_problem = str(problem)
    return _render(
        request,
        "project_odoo_database_bindings.html",
        project=project,
        workspaces=workspaces,
        selected_workspace_id=selected_id or "",
        selected_role=role.value,
        selected=selected,
        connection_problem=connection_problem,
        definition=definition,
        replacement=replacement,
        replacement_problem=replacement_problem,
        rebind_workspace_id=rebind_workspace_id or "",
        can_declare=_can_declare(identity, project_id),
        can_rebind=identity.authorization.authorizes(
            Capability.SOURCE_CONFIGURE, at=datetime.now(UTC), project_id=project_id
        ),
        operation_id=(values or {}).get("operation_id") or str(uuid4()),
        values=values or {},
        reload_url=_location(
            project_id,
            workspace_id=selected_id or "",
            role=role.value,
            saved_data_id=saved_data_id or "",
            rebind_workspace_id=rebind_workspace_id or "",
        ),
        error=error,
        status_code=status_code,
    )


def build_odoo_database_bindings_router(context: WebContext) -> APIRouter:
    """Build local, explicit database reset and saved-source rebind commands."""

    router = APIRouter()

    @router.get(
        "/projects/{project_id}/odoo-database-bindings", response_class=HTMLResponse
    )
    async def database_bindings_page(
        request: Request,
        project_id: str,
        workspace_id: str | None = None,
        role: str = "SOURCE",
        saved_data_id: str | None = None,
        rebind_workspace_id: str | None = None,
    ):
        require_session(request)
        identity = request_identity(request, context.identity_resolver)
        return await run_page_read(
            _page,
            request,
            context,
            project_id,
            identity=identity,
            workspace_id=workspace_id,
            role=_role(role),
            saved_data_id=saved_data_id,
            rebind_workspace_id=rebind_workspace_id,
        )

    @router.post("/projects/{project_id}/odoo-database-bindings/declare")
    async def declare_recreated_database(request: Request, project_id: str):
        form = await request.form()
        _single_form(
            request,
            form,
            {
                "csrf_token",
                "workspace_id",
                "role",
                "saved_data_id",
                "expected_generation_hash",
                "operation_id",
                "confirm_database",
                "confirm_recreated",
                "reason",
            },
        )
        identity = request_identity(request, context.identity_resolver)
        if not _can_declare(identity, project_id):
            raise HTTPException(
                403,
                "An organization-wide administrator must record this database reset",
            )
        workspace_id, role = _text(form, "workspace_id"), _role(_text(form, "role"))
        saved_data_id = _text(form, "saved_data_id") or None

        def declare():
            _project(context, project_id, identity, Capability.PROJECT_ADMIN)
            state = _owned_workspace(context, project_id, workspace_id, identity)
            connection = _known_connection(state, role)
            if _text(form, "confirm_recreated") != "yes":
                raise ValueError(
                    "Confirm that this Odoo database has already been recreated"
                )
            if _text(form, "confirm_database") != connection.database:
                raise ValueError("Type the exact database name shown in this review")
            reason = _text(form, "reason").strip()
            if not reason or len(reason) > 1000:
                raise ValueError(
                    "Enter a reason of up to 1,000 characters for recording this database reset"
                )
            context.odoo_database_bindings.declare_recreated(
                state.workspace_id,
                base_url=connection.base_url,
                database=connection.database,
                expected_generation_hash=_text(form, "expected_generation_hash"),
                operation_id=require_uuid(_text(form, "operation_id"), "operation_id"),
                reason=reason,
                actor=identity.actor,
                scope=identity.organization_scope,
                authorization_receipt=identity.authorization,
            )

        try:
            await run_local_operation(declare)
        except _SAFE_ERRORS as problem:
            return await run_page_read(
                _page,
                request,
                context,
                project_id,
                identity=identity,
                workspace_id=workspace_id,
                role=role,
                saved_data_id=saved_data_id,
                error=str(problem),
                values={
                    key: value
                    for key, value in form.items()
                    if isinstance(value, str) and key != "csrf_token"
                },
                status_code=409
                if isinstance(problem, OdooDatabaseGenerationConflict)
                else 422,
            )
        _flash(
            request,
            "Database reset recorded in Impodo. Start a fresh review before capturing or restoring data.",
        )
        return RedirectResponse(
            _location(
                project_id,
                workspace_id=workspace_id,
                role=role.value,
                saved_data_id=saved_data_id or "",
            ),
            303,
        )

    @router.post("/projects/{project_id}/saved-odoo-data/{saved_data_id}/rebind-source")
    async def rebind_saved_source(
        request: Request, project_id: str, saved_data_id: str
    ):
        form = await request.form()
        _single_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "workspace_id",
                "confirm_database",
                "confirm_company",
            },
        )
        identity = request_identity(request, context.identity_resolver)
        workspace_id = _text(form, "workspace_id")

        def rebind():
            _project(context, project_id, identity, Capability.SOURCE_CONFIGURE)
            saved = context.saved_odoo_data
            if saved is None:
                raise HTTPException(
                    503, "Saved Odoo data is unavailable in this deployment"
                )
            definition = saved.get(project_id, saved_data_id, actor=identity.actor)
            state = _owned_workspace(context, project_id, workspace_id, identity)
            connection = _known_connection(state, OdooDatabaseBindingRole.SOURCE)
            if _text(form, "confirm_database") != connection.database:
                raise ValueError("Type the exact reviewed source database name")
            if (
                state.odoo_company is None
                or _text(form, "confirm_company") != state.odoo_company.name
            ):
                raise ValueError("Type the exact reviewed source company name")
            return saved.rebind_source(
                project_id,
                saved_data_id,
                state.workspace_id,
                actor=identity.actor,
                expected_revision=_revision(form),
                all_fields=json.loads(definition.config_json).get("all_fields") is True,
            )

        try:
            await run_local_operation(rebind)
        except _SAFE_ERRORS as problem:
            return await run_page_read(
                _page,
                request,
                context,
                project_id,
                identity=identity,
                saved_data_id=saved_data_id,
                rebind_workspace_id=workspace_id,
                error=str(problem),
                values={
                    key: value
                    for key, value in form.items()
                    if isinstance(value, str) and key != "csrf_token"
                },
                status_code=422,
            )
        _flash(
            request,
            "The reviewed source is saved for future updates. Earlier saved versions remain available.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/saved-odoo-data?{urlencode({'selected': saved_data_id})}",
            303,
        )

    return router
