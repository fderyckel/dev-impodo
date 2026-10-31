"""Create protected storage for current Odoo company choices.

See docs/developer/workflow/02-odoo-data.md for protected company ownership.
"""

from __future__ import annotations

import duckdb


def create_odoo_company_bindings(connection: duckdb.DuckDBPyConnection) -> None:
    """Keep target-specific numeric company choices in the protected data plane."""
    connection.execute(
        "CREATE TABLE odoo_company_binding "
        "(binding_hash VARCHAR PRIMARY KEY, selection_json VARCHAR NOT NULL)"
    )
