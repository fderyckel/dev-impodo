"""Construct closed native write and read-back ports for one Odoo load."""

from __future__ import annotations

from ..connectors import (
    Json2Config,
    Json2WriteIdentityConnector,
    target_record_read_config,
)
from ..models import OdooWriteIdentity
from ..odoo_scope import OdooApiScope
from ..odoo_writer import Json2WriteExecutor, OdooWriteExecutor
from ..odoo_readback import Json2ReadbackReader, OdooReadbackReader
from ..projects import MigrationProject, ProjectError


def _probe_write_identity(
    project: MigrationProject,
    api_key: str,
    scope: OdooApiScope,
) -> OdooWriteIdentity:
    """Probe the separate load credential against the exact reviewed scope."""

    if not api_key.strip():
        raise ProjectError("Enter an Odoo API key for this load")
    connector = Json2WriteIdentityConnector(
        target_record_read_config(
            Json2Config(
                base_url=project.odoo_base_url,
                database=project.odoo_database,
                api_key=api_key,
                connection_mode=project.odoo_connection_mode.value,
                retries=0,
            )
        )
    )
    return connector.probe_write_identity(
        tuple(item.model for item in scope.models),
        tuple(item.model for item in scope.models if item.write_fields),
    )


def _write_executor(
    project: MigrationProject,
    api_key: str,
    scope: OdooApiScope,
) -> OdooWriteExecutor:
    """Bind the writer to the exact target and reviewed preview capability."""

    if not api_key.strip():
        raise ProjectError("Enter an Odoo API key for this load")
    return Json2WriteExecutor(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
            retries=0,
        ),
        scope,
    )


def _readback_reader(
    project: MigrationProject,
    api_key: str,
    scope: OdooApiScope,
) -> OdooReadbackReader:
    """Bind post-write verification to the same exact target."""

    if not api_key.strip():
        raise ProjectError("Enter an Odoo API key to verify this load")
    return Json2ReadbackReader(
        target_record_read_config(
            Json2Config(
                base_url=project.odoo_base_url,
                database=project.odoo_database,
                api_key=api_key,
                connection_mode=project.odoo_connection_mode.value,
                retries=0,
            )
        ),
        scope,
    )
