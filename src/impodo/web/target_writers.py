"""Construct the closed native writer for one confirmed local Odoo load."""

from __future__ import annotations

from ..connectors import Json2Config
from ..odoo_writer import Json2WriteExecutor, OdooWriteExecutor
from ..odoo_readback import Json2ReadbackReader, OdooReadbackReader
from ..projects import MigrationProject, OdooConnectionMode, ProjectError


def _write_executor(project: MigrationProject, api_key: str) -> OdooWriteExecutor:
    """Bind the practical writer to the project's exact disposable target."""

    if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
        raise ProjectError("The first load path is available only for Local Odoo")
    if not api_key.strip():
        raise ProjectError("Enter an Odoo API key for this load")
    return Json2WriteExecutor(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
            retries=0,
        )
    )


def _readback_reader(
    project: MigrationProject,
    api_key: str,
) -> OdooReadbackReader:
    """Bind post-write verification to the same exact local target."""

    if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
        raise ProjectError("Load verification is available only for Local Odoo")
    if not api_key.strip():
        raise ProjectError("Enter an Odoo API key to verify this load")
    return Json2ReadbackReader(
        Json2Config(
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            api_key=api_key,
            connection_mode=project.odoo_connection_mode.value,
            retries=0,
        )
    )
