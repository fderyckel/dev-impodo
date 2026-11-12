"""Verify the source-company migration preserves older capture evidence."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.schema.odoo_saved_source_company import (
    read_saved_source_company,
)
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError


class SavedSourceCompanyUpgradeTests(unittest.TestCase):
    def test_version_six_upgrade_is_atomic_and_preserves_capture_evidence(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("DROP TABLE odoo_saved_source_company")
            connection.execute("UPDATE schema_version SET version = 6")
            connection.execute(
                "INSERT INTO source_selection VALUES (1, 'immutable-source')"
            )
            upgrade = WORKSPACE_ENGINE_UPGRADES[6]

            def fail_after_creation(current):
                upgrade.apply(current)
                raise RuntimeError("injected upgrade interruption")

            with (
                patch.dict(
                    WORKSPACE_ENGINE_UPGRADES,
                    {
                        6: ForwardSchemaUpgrade(
                            upgrade.migration_id, fail_after_creation
                        )
                    },
                ),
                self.assertRaises(WorkspaceStateCompatibilityError),
            ):
                schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (6,),
            )
            self.assertNotIn(
                ("odoo_saved_source_company",),
                connection.execute("SHOW TABLES").fetchall(),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM source_selection").fetchall(),
                [(1, "immutable-source")],
            )
            schema._ensure_workspace_database_schema(connection)
            schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT * FROM source_selection").fetchall(),
                [(1, "immutable-source")],
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(),
                [(6, 7, "saved-odoo-source-company-v7")],
            )
            self.assertIsNone(read_saved_source_company(connection))

    def test_source_company_read_rejects_modified_binding(self):
        with duckdb.connect(":memory:") as connection:
            WorkspaceEngineSchemaMixin()._initialize_workspace_database(connection)
            company = OdooCompanySelection("sha256:" + "1" * 64, 7, "Source company")
            connection.execute(
                "INSERT INTO odoo_saved_source_company VALUES (1, ?, ?)",
                [company.binding_hash, company.to_json()],
            )
            self.assertEqual(read_saved_source_company(connection), company)
            connection.execute(
                "UPDATE odoo_saved_source_company SET binding_hash = ?",
                ["sha256:" + "0" * 64],
            )
            with self.assertRaisesRegex(WorkspaceError, "binding changed"):
                read_saved_source_company(connection)
