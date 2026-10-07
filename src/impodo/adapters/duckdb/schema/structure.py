"""Read ordered columns in one catalogue query for exact store validation."""

from __future__ import annotations

import duckdb


def read_table_columns(
    connection: duckdb.DuckDBPyConnection,
) -> dict[str, tuple[str, ...]]:
    """Keep the existing main-store column checks without per-table queries."""

    columns: dict[str, list[str]] = {}
    catalogs: dict[str, str] = {}
    for catalog, table, column in connection.execute(
        """
        SELECT database_name, table_name, column_name
          FROM duckdb_columns()
         WHERE (database_name = current_database() AND schema_name = current_schema())
            OR (database_name = 'temp' AND schema_name = 'main')
         ORDER BY table_name, (database_name = 'temp') DESC, column_index
        """
    ).fetchall():
        table = str(table)
        catalog = str(catalog)
        if catalogs.setdefault(table, catalog) == catalog:
            columns.setdefault(table, []).append(str(column))
    return {table: tuple(names) for table, names in columns.items()}
