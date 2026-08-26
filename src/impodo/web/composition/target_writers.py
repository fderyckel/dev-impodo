"""Construct closed native write and read-back ports for one Odoo load."""

from __future__ import annotations

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2WriteIdentityConnector,
    target_record_read_config,
)
from impodo.domain.shared.models import OdooWriteIdentity
from impodo.domain.execution.odoo_scope import OdooApiScope
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStateError


def _probe_write_identity(
    workspace_state: WorkspaceState,
    api_key: str,
    scope: OdooApiScope,
) -> OdooWriteIdentity:
    """Probe the separate load credential against the exact reviewed scope."""

    if not api_key.strip():
        raise WorkspaceStateError("Enter an Odoo API key for this load")
    connector = Json2WriteIdentityConnector(
        target_record_read_config(
            Json2Config(
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
                api_key=api_key,
                connection_mode=workspace_state.odoo_connection_mode.value,
                retries=0,
            )
        )
    )
    return connector.probe_write_identity(
        tuple(item.model for item in scope.models),
        tuple(item.model for item in scope.models if item.write_fields),
    )


def _write_executor(
    workspace_state: WorkspaceState,
    api_key: str,
    scope: OdooApiScope,
) -> OdooWriteExecutor:
    """Bind the writer to the exact target and reviewed preview capability."""

    if not api_key.strip():
        raise WorkspaceStateError("Enter an Odoo API key for this load")
    return Json2WriteExecutor(
        Json2Config(
            base_url=workspace_state.odoo_base_url,
            database=workspace_state.odoo_database,
            api_key=api_key,
            connection_mode=workspace_state.odoo_connection_mode.value,
            retries=0,
        ),
        scope,
    )


def _readback_reader(
    workspace_state: WorkspaceState,
    api_key: str,
    scope: OdooApiScope,
) -> OdooReadbackReader:
    """Bind post-write verification to the same exact target."""

    if not api_key.strip():
        raise WorkspaceStateError("Enter an Odoo API key to verify this load")
    return Json2ReadbackReader(
        target_record_read_config(
            Json2Config(
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
                api_key=api_key,
                connection_mode=workspace_state.odoo_connection_mode.value,
                retries=0,
            )
        ),
        scope,
    )

