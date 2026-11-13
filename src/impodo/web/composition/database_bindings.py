"""Admit reviewed database bindings and fence Odoo-source transport.

Only an explicit connection or saved-source review may create a binding.
Approvals and journals cannot create one merely by being reopened. Local
checks finish before transport, and every callback retains the exact immutable
binding captured at admission rather than adopting a later generation.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid5

from impodo.application.odoo_generation_fence import (
    GenerationFencedOdooReadbackReader,
    GenerationFencedOdooWriteExecutor,
)
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingError,
    OdooDatabaseBindingRole,
    database_scope_hash,
)
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.shared.tenancy import AuthorizationReceipt, RequestOrganizationScope
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState

from ..context import WebContext
from .page_reads import run_local_operation_sync


def review_database_binding(
    context: WebContext,
    workspace: WorkspaceState,
    role: OdooDatabaseBindingRole,
    *,
    actor: Actor,
    scope: RequestOrganizationScope,
    authorization_receipt: AuthorizationReceipt,
    base_url: str | None = None,
    database: str | None = None,
) -> OdooDatabaseBinding:
    """Bind a fresh reviewed workspace, refusing old approvals and journals."""

    endpoint = base_url if base_url is not None else workspace.odoo_base_url
    database_name = database if database is not None else workspace.odoo_database
    capability = (
        Capability.SOURCE_CONFIGURE
        if role is OdooDatabaseBindingRole.SOURCE
        else Capability.EXPORT_PLAN_APPROVE
    )

    def admit() -> OdooDatabaseBinding:
        service = context.odoo_database_bindings
        context.workspace_access.require(
            actor, capability, workspace_id=workspace.workspace_id
        )
        existing = service.get(
            workspace.workspace_id,
            role,
            base_url=endpoint,
            database=database_name,
            actor=actor,
            capability=capability,
        )
        if existing is not None:
            return service.require_current(
                workspace.workspace_id,
                role,
                base_url=endpoint,
                database=database_name,
                actor=actor,
                capability=capability,
            )
        if (
            context.execution.current_transfer_run(workspace.workspace_id) is not None
            or workspace.destination_match_plan is not None
            or workspace.transfer_review_package is not None
            or workspace.transfer_review_approval is not None
            or workspace.transfer_preflight_report is not None
            or (
                role is OdooDatabaseBindingRole.SOURCE
                and context.queries.get_source_selection(workspace.workspace_id)
                is not None
            )
        ):
            raise WorkspaceError(
                "This workspace has earlier source or load evidence without a reviewed "
                "database binding. Start a fresh source workspace or restore; the saved "
                "source data and load journal remain available."
            )
        current = service.current(
            workspace.workspace_id,
            base_url=endpoint,
            database=database_name,
            actor=actor,
            capability=capability,
            scope=scope,
            authorization_receipt=authorization_receipt,
        )
        return service.bind(
            workspace.workspace_id,
            role,
            base_url=endpoint,
            database=database_name,
            expected_generation_hash=current.generation_hash,
            operation_id=str(
                uuid5(
                    UUID(workspace.workspace_id),
                    f"database-review:{role.value}:{database_scope_hash(endpoint, database_name)}",
                )
            ),
            actor=actor,
            capability=capability,
            scope=scope,
            authorization_receipt=authorization_receipt,
        )

    try:
        return run_local_operation_sync(admit)
    except OdooDatabaseBindingError as error:
        raise WorkspaceError(str(error)) from error


def frozen_database_guard(
    context: WebContext,
    workspace: WorkspaceState,
    *,
    actor: Actor,
    role: OdooDatabaseBindingRole,
    capability: Capability,
) -> Callable[[], None]:
    """Capture exact authority once and recheck it without opening remote I/O."""

    def current() -> OdooDatabaseBinding:
        try:
            return context.odoo_database_bindings.require_current(
                workspace.workspace_id,
                role,
                base_url=workspace.odoo_base_url,
                database=workspace.odoo_database,
                actor=actor,
                capability=capability,
            )
        except OdooDatabaseBindingError as error:
            raise WorkspaceError(str(error)) from error

    binding = run_local_operation_sync(current)

    def check() -> None:
        actual = run_local_operation_sync(current)
        if actual.content_hash != binding.content_hash:
            raise WorkspaceError(
                "The reviewed database binding changed. Start a fresh restore or workspace."
            )

    return check


def fenced_transfer_writer(
    context: WebContext,
    workspace: WorkspaceState,
    delegate: OdooWriteExecutor,
    *,
    actor: Actor,
) -> OdooWriteExecutor:
    """Fence Odoo-source execution while preserving other admitted workflows."""

    if workspace.source_mode is not SourceMode.ODOO:
        return delegate
    return GenerationFencedOdooWriteExecutor(
        delegate,
        frozen_database_guard(
            context,
            workspace,
            actor=actor,
            role=OdooDatabaseBindingRole.DESTINATION,
            capability=Capability.EXPORT_PLAN_EXECUTE,
        ),
    )


def fenced_transfer_readback(
    context: WebContext,
    workspace: WorkspaceState,
    delegate: OdooReadbackReader,
    *,
    actor: Actor,
) -> OdooReadbackReader:
    """Prevent a replacement database from verifying a historical journal."""

    if workspace.source_mode is not SourceMode.ODOO:
        return delegate
    return GenerationFencedOdooReadbackReader(
        delegate,
        frozen_database_guard(
            context,
            workspace,
            actor=actor,
            role=OdooDatabaseBindingRole.DESTINATION,
            capability=Capability.EXPORT_PLAN_EXECUTE,
        ),
    )
