"""Bulk schema inspection must preserve ordered columns and fresh rejection."""

import unittest

import duckdb

from impodo.adapters.duckdb.schema.structure import read_table_columns
from impodo.adapters.duckdb.schema.workspace_engine import WorkspaceEngineSchemaMixin
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError


class SchemaStructureTests(unittest.TestCase):
    def test_bulk_columns_match_table_info_including_temporary_shadows(self):
        with duckdb.connect() as connection:
            connection.execute('CREATE TABLE "names with spaces" (z INTEGER, a VARCHAR)')
            connection.execute('CREATE TABLE shadowed (original INTEGER)')
            connection.execute('CREATE TEMP TABLE shadowed (replacement VARCHAR)')
            connection.execute("ATTACH ':memory:' AS unrelated")
            connection.execute('CREATE TABLE unrelated.shadowed (hidden INTEGER)')
            self.assertEqual(read_table_columns(connection), {
                "names with spaces": ("z", "a"), "shadowed": ("replacement",),
            })
            self.assertEqual(
                read_table_columns(connection)["shadowed"],
                tuple(row[1] for row in connection.execute("PRAGMA table_info(shadowed)").fetchall()),
            )

    def test_validation_rejects_a_changed_structure_on_the_same_connection(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect() as connection:
            schema._initialize_workspace_database(connection)
            schema._ensure_workspace_database_schema(connection)
            connection.execute("ALTER TABLE audit_event ADD COLUMN unexpected INTEGER")
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)

    def test_validation_rejects_a_temporary_table_shadowing_required_columns(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect() as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("CREATE TEMP TABLE audit_event (unexpected INTEGER)")
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)
