from __future__ import annotations

import unittest

import duckdb

from impodo.adapters.duckdb.constants import SCHEMA_GENERATION, SCHEMA_VERSION
from impodo.adapters.duckdb.schema.workspace_engine import WorkspaceEngineSchemaMixin
from impodo.workspace_state import WorkspaceStateCompatibilityError


class WorkspaceSchemaContractTests(unittest.TestCase):
    def test_m7_version_two_uses_only_the_current_operational_tables(self) -> None:
        schema = WorkspaceEngineSchemaMixin()
        connection = duckdb.connect(":memory:")
        try:
            schema._initialize_workspace_database(connection)

            self.assertEqual(
                connection.execute(
                    "SELECT generation, version FROM schema_version"
                ).fetchone(),
                (SCHEMA_GENERATION, SCHEMA_VERSION),
            )
            self.assertEqual(SCHEMA_VERSION, 2)
            tables = {
                item[0] for item in connection.execute("SHOW TABLES").fetchall()
            }
            self.assertIn("supporting_lookup_revision", tables)
            self.assertIn("supporting_lookup_current", tables)
            self.assertNotIn("project_schema_migration", tables)
        finally:
            connection.close()

    def test_version_one_with_the_removed_ledger_is_rejected(self) -> None:
        schema = WorkspaceEngineSchemaMixin()
        connection = duckdb.connect(":memory:")
        try:
            schema._initialize_workspace_database(connection)
            connection.execute(
                """
                CREATE TABLE project_schema_migration (
                    migration_id VARCHAR PRIMARY KEY,
                    checksum VARCHAR NOT NULL,
                    applied_at VARCHAR NOT NULL
                )
                """
            )
            connection.execute("UPDATE schema_version SET version = 1")
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
