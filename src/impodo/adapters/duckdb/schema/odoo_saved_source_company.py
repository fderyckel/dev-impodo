"""Own the optional source company separately from the restore destination."""

from __future__ import annotations

import duckdb

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.errors import WorkspaceError

SAVED_SOURCE_COMPANY_COLUMNS = ("singleton_id", "binding_hash", "company_json")


def create_saved_source_company_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Add source-owned context without changing existing destination bindings."""

    connection.execute(
        "CREATE TABLE odoo_saved_source_company ("
        "singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), "
        "binding_hash VARCHAR NOT NULL, company_json VARCHAR NOT NULL)"
    )


def read_saved_source_company(
    connection: duckdb.DuckDBPyConnection,
) -> OdooCompanySelection | None:
    """Read and verify one protected source company on the current owner."""

    row = connection.execute(
        "SELECT binding_hash, company_json FROM odoo_saved_source_company WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        return None
    selection = OdooCompanySelection.from_json(str(row[1]))
    if selection.binding_hash != str(row[0]):
        raise WorkspaceError("The saved Odoo source company binding changed")
    return selection
