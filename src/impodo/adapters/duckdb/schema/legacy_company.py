"""Preserve the known development company layout during a one-way upgrade.

See docs/developer/workflow/02-odoo-data.md for protected company ownership.
"""

from __future__ import annotations

import duckdb

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.shared.models import target_identity_hash

from .structure import read_table_columns

LEGACY_COMPANY_MIGRATION_ID = "workspace-engine-v23-to-v24-odoo-operation-company"
COMPANY_BINDING_COLUMNS = ("binding_hash", "selection_json")


def create_odoo_company_bindings(connection: duckdb.DuckDBPyConnection) -> None:
    """Keep target-specific numeric company choices in the protected data plane."""
    connection.execute(
        "CREATE TABLE odoo_company_binding "
        "(binding_hash VARCHAR PRIMARY KEY, selection_json VARCHAR NOT NULL)"
    )


def upgrade_legacy_company_layout(
    connection: duckdb.DuckDBPyConnection,
    *,
    workspace_columns: tuple[str, ...],
) -> None:
    """Normalize only the exact earlier layout; final schema validation still applies.

    The caller owns the complete forward-upgrade transaction. A saved legacy
    choice survives as protected evidence, without inventing a canonical run
    confirmation or changing historical approvals and load journals.
    """
    columns = read_table_columns(connection)
    actual = columns.get("workspace_projection_cache", ())
    if (
        actual == workspace_columns
        and columns.get("odoo_company_binding") == COMPANY_BINDING_COLUMNS
    ):
        return
    if (
        actual != (*workspace_columns, "odoo_company_id", "odoo_company_name")
        or "odoo_company_binding" in columns
        or connection.execute(
            "SELECT from_version, migration_id FROM schema_migration WHERE to_version = 24"
        ).fetchone()
        != (23, LEGACY_COMPANY_MIGRATION_ID)
        or connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'workspace_projection_cache' "
            "AND column_name IN ('odoo_company_id', 'odoo_company_name') "
            "ORDER BY ordinal_position"
        ).fetchall()
        != [("odoo_company_id", "BIGINT"), ("odoo_company_name", "VARCHAR")]
    ):
        raise ValueError("Unrecognized historical company schema layout")

    rows = connection.execute(
        "SELECT odoo_connection_mode, odoo_base_url, odoo_database, "
        "odoo_company_id, odoo_company_name FROM workspace_projection_cache"
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("Historical company setup has more than one workspace row")
    selections = []
    for mode, url, database, identifier, name in rows:
        if identifier is None:
            if name not in (None, ""):
                raise ValueError("Historical company name has no identifier")
            continue
        if mode not in {"LOCAL", "REMOTE"} or not url or not database:
            raise ValueError("Historical company choice has no exact Odoo target")
        selections.append(
            OdooCompanySelection(
                target_identity_hash(
                    connection_mode=mode, base_url=url, database=database
                ),
                identifier,
                name,
            )
        )

    create_odoo_company_bindings(connection)
    for selection in selections:
        connection.execute(
            "INSERT INTO odoo_company_binding VALUES (?, ?)",
            [selection.binding_hash, selection.to_json()],
        )
    connection.execute(
        "ALTER TABLE workspace_projection_cache DROP COLUMN odoo_company_id"
    )
    connection.execute(
        "ALTER TABLE workspace_projection_cache DROP COLUMN odoo_company_name"
    )
