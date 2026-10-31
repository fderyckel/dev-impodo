"""Upgrade the protected relationship index without rewriting saved evidence."""

import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError


class RelationshipChoiceUpgradeTests(unittest.TestCase):
    def test_version_two_upgrade_preserves_saved_proofs_and_rolls_back_failure(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute(
                "DROP TABLE matching_order_protected_relationship_choice"
            )
            connection.execute("UPDATE schema_version SET version = 2")
            connection.execute(
                "INSERT INTO matching_order_protected_snapshot VALUES ('check', 'unchanged-hash', 'unchanged-protected-values')"
            )
            before = connection.execute(
                "SELECT * FROM matching_order_protected_snapshot"
            ).fetchall()
            upgrade = WORKSPACE_ENGINE_UPGRADES[2]

            def failed(current):
                upgrade.apply(current)
                raise RuntimeError("injected after index creation")

            with patch.dict(
                WORKSPACE_ENGINE_UPGRADES,
                {2: ForwardSchemaUpgrade(upgrade.migration_id, failed)},
            ):
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (2,),
            )
            self.assertNotIn(
                ("matching_order_protected_relationship_choice",),
                connection.execute("SHOW TABLES").fetchall(),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(), []
            )
            schema._ensure_workspace_database_schema(connection)
            schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT * FROM matching_order_protected_snapshot"
                ).fetchall(),
                before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM matching_order_protected_relationship_choice"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_version, to_version FROM schema_migration"
                ).fetchall(),
                [(2, 3)],
            )
