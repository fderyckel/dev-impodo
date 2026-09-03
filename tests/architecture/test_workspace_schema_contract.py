from __future__ import annotations

import unittest

import duckdb

from impodo.adapters.duckdb.constants import SCHEMA_GENERATION, SCHEMA_VERSION
from impodo.adapters.duckdb.schema.workspace_engine import WorkspaceEngineSchemaMixin
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError


class WorkspaceSchemaContractTests(unittest.TestCase):
    def test_current_generation_uses_only_workspace_identity_shapes(self) -> None:
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
            self.assertEqual(SCHEMA_VERSION, 9)
            tables = {
                item[0] for item in connection.execute("SHOW TABLES").fetchall()
            }
            self.assertIn("supporting_lookup_revision", tables)
            self.assertIn("supporting_lookup_current", tables)
            self.assertIn("workspace_projection_cache", tables)
            self.assertIn("schema_migration", tables)
            self.assertIn("mapping_mutation_receipt", tables)
            self.assertNotIn("workspace_state", tables)
            self.assertNotIn("project_schema_migration", tables)
            audit_columns = tuple(
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('audit_event')"
                ).fetchall()
            )
            self.assertIn("workspace_revision", audit_columns)
            self.assertNotIn("project_revision", audit_columns)
        finally:
            connection.close()

    def test_retired_generation_is_rejected_without_upgrade(self) -> None:
        schema = WorkspaceEngineSchemaMixin()
        connection = duckdb.connect(":memory:")
        try:
            schema._initialize_workspace_database(connection)
            connection.execute(
                "UPDATE schema_version SET generation = ?",
                ["impodo-workspace-engine-retired-generation"],
            )
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)
        finally:
            connection.close()

    def test_mixed_retired_table_shape_is_rejected(self) -> None:
        schema = WorkspaceEngineSchemaMixin()
        connection = duckdb.connect(":memory:")
        try:
            schema._initialize_workspace_database(connection)
            connection.execute(
                "CREATE TABLE workspace_state (project_id VARCHAR)"
            )
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
