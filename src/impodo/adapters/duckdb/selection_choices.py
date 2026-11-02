"""Publish selection-only schema transitions inside mapping transactions."""

from __future__ import annotations

import duckdb

from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.selection_choices import SelectionSchemaRefresh


def publish_selection_refresh(
    connection: duckdb.DuckDBPyConnection, refresh: SelectionSchemaRefresh
) -> None:
    """Recheck current evidence and publish a reviewed selection projection."""
    row = connection.execute(
        "SELECT catalog_json FROM odoo_schema_catalog WHERE singleton_id = 1"
    ).fetchone()
    current = OdooSchemaCatalog.from_json(str(row[0])) if row else None
    governance_row = connection.execute(
        "SELECT revision.governance_json FROM schema_governance_current AS current "
        "JOIN schema_governance_revision AS revision USING (governance_id, version) WHERE current.singleton_id = 1"
    ).fetchone()
    governance = (
        SchemaGovernance.from_json(str(governance_row[0])) if governance_row else None
    )
    working = connection.execute("SELECT version FROM mapping_working_draft WHERE singleton_id = 1").fetchone()
    if (current != refresh.previous_schema or governance != refresh.previous_governance
        or (int(working[0]) if working else None) != refresh.working_version):
        raise WorkspaceError(
            "The Odoo choices changed in another request. Refresh them again."
        )
    connection.execute(
        "UPDATE odoo_schema_catalog SET catalog_json = ? WHERE singleton_id = 1",
        [refresh.schema.to_json()],
    )
    if refresh.governance is not None and refresh.governance != governance:
        updated = refresh.governance
        connection.execute(
            "INSERT INTO schema_governance_revision VALUES (?, ?, ?, ?, ?)",
            [
                updated.governance_id,
                updated.version,
                updated.catalog_hash,
                updated.content_hash,
                updated.to_json(),
            ],
        )
        connection.execute(
            "INSERT OR REPLACE INTO schema_governance_current VALUES (1, ?, ?)",
            [updated.governance_id, updated.version],
        )
