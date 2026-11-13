"""Recover historical registry v2 layouts without replacing saved evidence."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.schema import migration_registry
from impodo.adapters.duckdb.schema.migration_registry import (
    MIGRATION_REGISTRY_GENERATION,
    MIGRATION_REGISTRY_VERSION,
    ensure_migration_registry_schema,
)
from impodo.adapters.duckdb.schema.structure import read_table_columns
from impodo.domain.project.foundation import MigrationStorageCompatibilityError

_POST_V2_TABLES = (
    "saved_odoo_source_binding",
    "odoo_workspace_database_binding",
    "odoo_database_generation_current",
    "odoo_database_generation",
    "saved_odoo_capture_operation",
)


def _rows(connection: duckdb.DuckDBPyConnection) -> dict[str, list[tuple]]:
    """Compare every stored value independently of physical row order."""

    return {
        table: sorted(
            connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr
        )
        for table in read_table_columns(connection)
    }


def _historical_registry(
    connection: duckdb.DuckDBPyConnection,
    *,
    missing_profile: bool,
    upgraded_from_v1: bool = True,
) -> dict[str, list[tuple]]:
    """Retain representative protected rows in either observed v2 layout."""

    ensure_migration_registry_schema(connection, Path("registry.duckdb"))
    connection.execute(
        """
        INSERT INTO migration_project_identity VALUES ('project');
        INSERT INTO saved_odoo_data_identity VALUES ('saved-data', 'project');
        INSERT INTO saved_odoo_data_definition VALUES (
            'saved-data', 'project', 1, 1, NULL,
            '{"label":"Fictional chart","odoo_major":20}', 'definition-hash'
        );
        INSERT INTO saved_odoo_data_configuration VALUES (
            'saved-data', 1,
            '{"label":"Fictional chart","odoo_major":20}', 'configuration-hash'
        )
        """
    )
    if not missing_profile:
        connection.execute(
            """
            INSERT INTO saved_odoo_restore_profile VALUES (
                'profile', 'project', 'saved-data', 1, NULL,
                '{"label":"Fictional destination"}', 'profile-hash'
            )
            """
        )
    for table in _POST_V2_TABLES:
        connection.execute(f'DROP TABLE "{table}"')
    expected = _rows(connection)
    if missing_profile:
        connection.execute("DROP TABLE saved_odoo_restore_profile")
    connection.execute("UPDATE schema_version SET version = 2")
    if upgraded_from_v1:
        connection.execute(
            "INSERT INTO schema_migration VALUES (1, 2, 'saved-odoo-data-and-restore-v2')"
        )
    return {
        table: rows
        for table, rows in expected.items()
        if table not in {"schema_version", "schema_migration"}
    }


class RegistryRestoreProfileUpgradeTests(unittest.TestCase):
    def test_both_v2_layouts_upgrade_once_and_preserve_all_business_rows(self) -> None:
        for missing_profile in (True, False):
            for upgraded_from_v1 in (True, False):
                with (
                    self.subTest(
                        missing_profile=missing_profile,
                        upgraded_from_v1=upgraded_from_v1,
                    ),
                    duckdb.connect(":memory:") as connection,
                ):
                    expected = _historical_registry(
                        connection,
                        missing_profile=missing_profile,
                        upgraded_from_v1=upgraded_from_v1,
                    )
                    for _ in range(2):
                        ensure_migration_registry_schema(
                            connection, Path("registry.duckdb")
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT * FROM schema_version"
                            ).fetchall(),
                            [
                                (
                                    1,
                                    MIGRATION_REGISTRY_GENERATION,
                                    MIGRATION_REGISTRY_VERSION,
                                )
                            ],
                        )
                        rows = _rows(connection)
                        self.assertEqual(
                            {table: rows[table] for table in expected}, expected
                        )
                        for table in _POST_V2_TABLES:
                            self.assertEqual(rows[table], [])
                        ledger = (
                            [(1, 2, "saved-odoo-data-and-restore-v2")]
                            if upgraded_from_v1
                            else []
                        )
                        self.assertEqual(
                            sorted(rows["schema_migration"]),
                            [
                                *ledger,
                                (2, 3, "saved-odoo-restore-profile-v3"),
                                (3, 4, "saved-odoo-capture-recovery-v4"),
                                (4, 5, "odoo-database-generation-authority-v5"),
                            ],
                        )

    def test_other_v2_layout_changes_are_rejected_without_mutation(self) -> None:
        for missing_profile, corrupt_sql in (
            (True, "DROP TABLE production_run_values"),
            (True, "CREATE TABLE unrelated (value VARCHAR)"),
            (
                True,
                "ALTER TABLE saved_odoo_data_configuration ADD COLUMN unexpected VARCHAR",
            ),
            (
                False,
                "ALTER TABLE saved_odoo_restore_profile ADD COLUMN unexpected VARCHAR",
            ),
        ):
            with (
                self.subTest(missing_profile=missing_profile, corruption=corrupt_sql),
                duckdb.connect(":memory:") as connection,
            ):
                _historical_registry(connection, missing_profile=missing_profile)
                connection.execute(corrupt_sql)
                before = _rows(connection)
                columns = read_table_columns(connection)
                with self.assertRaises(MigrationStorageCompatibilityError):
                    ensure_migration_registry_schema(
                        connection, Path("registry.duckdb")
                    )
                self.assertEqual(read_table_columns(connection), columns)
                self.assertEqual(_rows(connection), before)

    def test_failure_after_creating_profile_rolls_back_ddl_version_and_ledger(
        self,
    ) -> None:
        with duckdb.connect(":memory:") as connection:
            _historical_registry(connection, missing_profile=True)
            before = _rows(connection)
            columns = read_table_columns(connection)
            create_profile = migration_registry._create_saved_odoo_restore_profile

            def fail_after_create(current: duckdb.DuckDBPyConnection) -> None:
                create_profile(current)
                raise RuntimeError("Injected failure after profile creation")

            with (
                patch.object(
                    migration_registry,
                    "_create_saved_odoo_restore_profile",
                    side_effect=fail_after_create,
                ),
                self.assertRaises(MigrationStorageCompatibilityError),
            ):
                ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            self.assertEqual(read_table_columns(connection), columns)
            self.assertEqual(_rows(connection), before)

    def test_current_version_with_missing_profile_is_rejected_without_repair(
        self,
    ) -> None:
        with duckdb.connect(":memory:") as connection:
            ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            connection.execute("DROP TABLE saved_odoo_restore_profile")
            before = _rows(connection)
            columns = read_table_columns(connection)
            with self.assertRaises(MigrationStorageCompatibilityError):
                ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            self.assertEqual(read_table_columns(connection), columns)
            self.assertEqual(_rows(connection), before)


if __name__ == "__main__":
    unittest.main()
