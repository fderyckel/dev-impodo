"""Reuse structure checks without reusing failed checks or requested lineage."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.schema.migration_workspace_store import (
    ensure_migration_workspace_store,
    initialize_migration_workspace_store,
)
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory, retain_databases_for_operation
from impodo.domain.project.foundation import MigrationStorageCompatibilityError
from tests.integration.duckdb.test_forward_upgrades import _workspace
from tests.support.database_probe import count_duckdb_statements


class ScopedStoreValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "workspace.duckdb"
        self.workspace = _workspace()
        self.factory = DuckDbConnectionFactory()
        with self.factory.connect(self.path) as connection:
            initialize_migration_workspace_store(connection, self.workspace)

    def check(self, workspace=None, path=None):
        with self.factory.connect(path or self.path) as connection:
            ensure_migration_workspace_store(connection, path or self.path, workspace or self.workspace)

    def test_reuse_counts_all_structural_work_but_checks_lineage_each_time(self):
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            with retain_databases_for_operation():
                self.check()
                self.check()
                with self.assertRaises(MigrationStorageCompatibilityError):
                    self.check(replace(self.workspace, project_id="50000000-0000-4000-8000-000000000001"))
                with self.factory.connect(self.path) as connection:
                    connection.execute("UPDATE workspace_linkage SET project_id = 'changed'")
                with self.assertRaises(MigrationStorageCompatibilityError):
                    self.check()
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 1)
        self.assertEqual(sum("duckdb_columns()" in sql for sql in statements), 1)
        self.assertEqual(sum("FROM workspace_linkage" in sql for sql in statements), 4)

    def test_next_operation_revalidates_structure_and_different_store(self):
        other = self.path.with_name("other.duckdb")
        with self.factory.connect(other) as connection:
            initialize_migration_workspace_store(connection, self.workspace)
            connection.execute("ALTER TABLE workspace_linkage ADD COLUMN unexpected INTEGER")
        with retain_databases_for_operation():
            self.check()
            with self.assertRaises(MigrationStorageCompatibilityError):
                self.check(path=other)
        with self.factory.connect(self.path) as connection:
            connection.execute("ALTER TABLE workspace_linkage ADD COLUMN unexpected INTEGER")
        with retain_databases_for_operation(), self.assertRaises(MigrationStorageCompatibilityError):
            self.check()

    def test_failed_structure_validation_is_not_retained(self):
        with self.factory.connect(self.path) as connection:
            connection.execute("ALTER TABLE workspace_linkage ADD COLUMN unexpected INTEGER")
        with collect_duckdb_request_timings() as timings, retain_databases_for_operation():
            with self.assertRaises(MigrationStorageCompatibilityError):
                self.check()
            with self.factory.connect(self.path) as connection:
                connection.execute("ALTER TABLE workspace_linkage DROP COLUMN unexpected")
            self.check()
        self.assertEqual(timings.schema_check_count, 2)
